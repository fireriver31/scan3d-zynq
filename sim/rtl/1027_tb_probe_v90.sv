`timescale 1ns/1ps
// Faults are intentional assumptions in this digital model, NOT observations
// of the real board. Clock/reset timing parameters match the shipped design.
module gt_model_v1 #(parameter ADDR=7'h5d, ID=24'h313139,
    parameter POWERED=1, REJECT_CONFIG=0, REJECT_READ=0)
    (input scl,input sda,input resetn,output reg low=0);
 reg [15:0] address=0;event start;
 always @(negedge sda)
   if(scl===1'b1 && !low && POWERED && resetn)->start;
 function [7:0] value(input [15:0] a);
   case(a)
    16'h8140:value=ID[7:0];16'h8141:value=ID[15:8];16'h8142:value=ID[23:16];
    16'h8048:value=8'h20;16'h8049:value=8'h03;
    16'h804a:value=8'he0;16'h804b:value=8'h01;
    16'h814e:value=0;default:value=0;
   endcase
 endfunction
 task automatic recv(output reg [7:0] b);
   integer j;begin b=0;
     for(j=0;j<8;j=j+1)begin @(posedge scl);b={b[6:0],sda};end
   end
 endtask
 task automatic ack(input ok);
   begin @(negedge scl);low=ok;@(posedge scl);@(negedge scl);low=0;end
 endtask
 task automatic send(input [7:0] b);
   integer j;begin
     for(j=7;j>=0;j=j-1)begin low=~b[j];@(posedge scl);@(negedge scl);end
     low=0;@(posedge scl);
     if(sda!==1)$fatal(1,"final byte missing master NACK");
     @(negedge scl);
   end
 endtask
 task automatic transaction;
   reg [7:0] dev,hi,lo,data;reg ok;begin
     recv(dev);ok=(dev[7:1]==ADDR)&&!(dev[0]&&REJECT_READ);ack(ok);
     if(!ok)forever @(posedge scl);
     if(dev[0])send(value(address));
     else begin
       recv(hi);ack(1);recv(lo);address={hi,lo};
       ok=!(REJECT_CONFIG && address==16'h8048);ack(ok);
       if(!ok)forever @(posedge scl);
       recv(data);ack(1);
     end
     forever @(posedge scl);
   end
 endtask
 initial begin
   @(start);
   forever begin
     fork transaction();begin @(start);end begin @(negedge resetn);end join_any
     disable fork;low=0;
   end
 end
endmodule

module fault_node_v1 #(parameter CASE_ID=0)(input clk,input resetn,
    output online,output [31:0] errors,output [23:0] product,
    output [15:0] max_x,output [15:0] max_y);
 tri1 mscl,msda,intpin;
 wire cscl,csda,chip_low;
 wire chip_reset,master_reset,selected;wire down;wire [3:0] points;
 wire [15:0] x,y;wire [31:0] seq;
 // This slave never drives SCL. Explicit open-drain intent avoids feedback
 // through pass-switch primitives in the simulator's switch-level model.
 assign cscl=(CASE_ID==4)?1'b1:mscl;
 assign csda=(CASE_ID==3)?1'b1:msda;
 assign msda=(CASE_ID!=3 && chip_low)?1'b0:1'bz;
 assign msda=(CASE_ID==5)?1'b0:1'bz;
 assign mscl=(CASE_ID==6)?1'b0:1'bz;
 assign chip_reset=(CASE_ID==10)?1'b0:master_reset;
 gt_model_v1 #(.ADDR(CASE_ID==1?7'h14:7'h5d),
   .ID(CASE_ID==7?24'h0:CASE_ID==8?24'h373239:24'h313139),
   .POWERED(CASE_ID!=2),.REJECT_CONFIG(CASE_ID==9),.REJECT_READ(CASE_ID==11)) chip(
   .scl(cscl),.sda(csda),.resetn(chip_reset),.low(chip_low));
 gt911_reader #(.CLK_HZ(50000000),.BUS_HZ(100000),.TIME_SCALE(1)) dut(
   .clk(clk),.rst_n(resetn),.tp_scl(mscl),.tp_sda(msda),.tp_rst(master_reset),
   .tp_int(intpin),.online(online),.down(down),.points(points),.x(x),.y(y),
   .touch_seq(seq),.product(product),.max_x(max_x),.max_y(max_y),
   .errors(errors),.address_sel(selected));
 integer trace;reg [4:0] previous=31;
 initial trace=$fopen($sformatf("out/1027_case_%0d_trace_v1.csv",CASE_ID),"w");
 always @(posedge clk)if(resetn && dut.bus.state!=previous)begin
   $fwrite(trace,"%0t,%0d,%0d,%0d,%b,%b,%h,%b,%h\n",$time,
     dut.state,dut.bus.state,dut.bus.byte_no,mscl,msda,
     dut.reg_addr,selected,product);
   previous<=dut.bus.state;
 end
endmodule

module tb_probe_v90;
 reg clk=0;always #10 clk=~clk; // Actual 50MHz, no scaled reset timers.
 reg resetn=0;wire [11:0] online;
 wire [31:0] errors[0:11];wire [23:0] product[0:11];
 wire [15:0] max_x[0:11],max_y[0:11];
 wire [31:0] bank_reader[0:11],bank_bus[0:11],bank_id[0:11];
 genvar j;generate for(j=0;j<12;j=j+1)begin:nodes
   fault_node_v1 #(.CASE_ID(j)) node(clk,resetn,online[j],errors[j],product[j],max_x[j],max_y[j]);
   assign bank_reader[j]=node.dut.diag_5d_reader;
   assign bank_bus[j]=node.dut.diag_5d_bus;
   assign bank_id[j]=node.dut.diag_5d_id;
 end endgenerate
 integer k,file;string name[0:11];
 initial begin
   name[0]="normal_5d";name[1]="normal_14";name[2]="absent_or_unpowered";
   name[3]="SDA_open";name[4]="SCL_open";name[5]="SDA_held_low";
   name[6]="SCL_held_low";name[7]="ACK_but_ID_zero";name[8]="ACK_but_other_ID";
   name[9]="config_address_NACK";name[10]="device_held_reset";name[11]="read_address_NACK";
   #100;resetn=1;
   #230000000;
   file=$fopen("out/1027_fault_results_v1.csv","w");
   $fdisplay(file,"case,online,errors,product_hex,max_x,max_y");
   for(k=0;k<12;k=k+1)begin
     $display("CASE %0d %s online=%b errors=%0d product=%h range=%0dx%0d",
       k,name[k],online[k],errors[k],product[k],max_x[k],max_y[k]);
     $fdisplay(file,"%s,%0d,%0d,%h,%0d,%0d",name[k],online[k],errors[k],product[k],max_x[k],max_y[k]);
     if(k<2)begin
       if(!online[k] || product[k]!==24'h313139 || max_x[k]!=800 || max_y[k]!=480)
         $fatal(1,"valid device failed case %0d",k);
     end else if(online[k] || errors[k]<1)
       $fatal(1,"fault did not report offline/bounded retries case %0d",k);
   end
   $fclose(file);
   file=$fopen("out/1027_diagnostic_results_v90.csv","w");
   $fdisplay(file,"case,bank_5d_reader,bank_5d_bus,bank_5d_id");
   for(k=0;k<12;k=k+1)begin
     $fdisplay(file,"%s,%h,%h,%h",name[k],bank_reader[k],bank_bus[k],bank_id[k]);
     if(k==0)begin
       if(bank_reader[k]!==0)$fatal(1,"healthy 5d false failure");
     end else if(k==1||k==2||k==3||k==4||k==10)begin
       if(bank_bus[k][7:0]!=2 || bank_reader[k][31:16]!=16'h8140)
         $fatal(1,"wrong write address NACK diagnosis %0d",k);
     end else if(k==5||k==6)begin
       if(bank_bus[k][7:0]!=1 || bank_bus[k][12:8]!=1)
         $fatal(1,"wrong start timeout %0d",k);
       if(k==5 && bank_bus[k][19:18]!=2'b01)$fatal(1,"SDA low levels lost");
       if(k==6 && bank_bus[k][19:18]!=2'b10)$fatal(1,"SCL low levels lost");
     end else if(k==7||k==8)begin
       if(bank_reader[k][4:0]!=2 || bank_id[k][26:24]!=7)
         $fatal(1,"ID rejection not retained %0d",k);
       if(k==7 && bank_id[k][23:0]!==0)$fatal(1,"zero ID changed");
       if(k==8 && bank_id[k][23:0]!==24'h373239)$fatal(1,"other ID lost during reset");
     end else if(k==9)begin
       if(bank_reader[k][31:16]!=16'h8048 || bank_bus[k][7:0]!=4 || bank_id[k][23:0]!=24'h313139)
         $fatal(1,"config failure or previously valid ID lost");
     end else if(k==11)begin
       if(bank_bus[k][7:0]!=5 || !bank_bus[k][22])$fatal(1,"read address NACK not isolated");
     end
   end
   $fclose(file);
   $display("PASS 12 diagnostic scenarios, failures retained per address, native 50MHz");
   $finish;
 end
 initial begin #240000000;$fatal(1,"global simulation timeout");end
endmodule
