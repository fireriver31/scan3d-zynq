`timescale 1ns/1ps
module tb_touch;
 reg clk=0;always #5 clk=~clk;
 reg resetn=0;tri1 scl,sda,intpin;wire rst,online,down,sel;
 wire [3:0] points;wire [15:0] x,y,mx,my;wire [31:0] seq,errors;wire [23:0] product;
 reg slave_low=0,stretch_low=0;assign sda=slave_low?1'b0:1'bz;
 assign scl=stretch_low?1'b0:1'bz;
 reg [7:0] status=8'h81;reg [15:0] xx=16'h029a,yy=16'h0154;
 reg [15:0] regaddr=0;integer clears=0,reads=0;event start;
 always @(negedge sda)if(scl===1'b1 && !slave_low)->start;
 gt911_reader #(.CLK_HZ(1000000),.BUS_HZ(100000),.TIME_SCALE(100)) dut(
  .clk(clk),.rst_n(resetn),.tp_scl(scl),.tp_sda(sda),.tp_rst(rst),.tp_int(intpin),
  .online(online),.down(down),.points(points),.x(x),.y(y),.touch_seq(seq),
  .product(product),.max_x(mx),.max_y(my),.errors(errors),.address_sel(sel));
 function [7:0] value(input [15:0] a);
  case(a)
   16'h8140:value="9";16'h8141:value="1";16'h8142:value="1";
   16'h8048:value=8'h20;16'h8049:value=8'h03; // 800
   16'h804a:value=8'he0;16'h804b:value=8'h01; // 480
   16'h814e:value=status;
   16'h814f:value=8'h07; // ID, deliberately not an X byte
   16'h8150:value=xx[7:0];16'h8151:value=xx[15:8];
   16'h8152:value=yy[7:0];16'h8153:value=yy[15:8];
   16'h8154:value=8'hbb;default:value=8'he7;
  endcase
 endfunction
 task automatic recv(output reg [7:0] b);
  integer j;begin b=0;for(j=0;j<8;j=j+1)begin @(posedge scl);b={b[6:0],sda};end end
 endtask
 task automatic acknowledge(input ok);
  begin @(negedge scl);slave_low=ok;@(posedge scl);@(negedge scl);slave_low=0;end
 endtask
 task automatic send(input [7:0] b);
  integer j;begin
   for(j=7;j>=0;j=j-1)begin slave_low=~b[j];@(posedge scl);@(negedge scl);end
   slave_low=0;@(posedge scl);if(sda!==1)$fatal(1,"master did not NACK final byte");
   @(negedge scl);
  end
 endtask
 task automatic transaction;
  reg [7:0] dev,hi,lo,b;begin
   recv(dev);acknowledge(dev[7:1]==7'h5d);
   if(dev[7:1]!=7'h5d)begin forever @(posedge clk);end
   if(dev[0])begin reads=reads+1;send(value(regaddr));end
   else begin
    recv(hi);acknowledge(1);recv(lo);acknowledge(1);regaddr={hi,lo};
    recv(b);acknowledge(1);
    if(regaddr==16'h814e)begin
     if(b!=0)$fatal(1,"status clear not zero");status=0;clears=clears+1;
    end
   end
   forever @(posedge clk);
  end
 endtask
 initial begin
  wait(resetn);wait(dut.bus.state==4);stretch_low=1;
  repeat(40)@(posedge clk);@(negedge clk);stretch_low=0;
 end
 initial begin
  @(start);
  forever begin
   fork transaction();begin @(start);end join_any
   disable fork;slave_low=0;
  end
 end
 initial begin
  #50;resetn=1;
  wait(down);#1;
  if(x!==16'h029a||y!==16'h0154||points!=1||!online||mx!=800||my!=480)
   $fatal(1,"wrong coherent first snapshot x=%h y=%h max=%d,%d",x,y,mx,my);
  if(product!==24'h313139)$fatal(1,"ID mismatch");
  $display("TOUCH correct 8150..8153 x=%d y=%d seq=%d",x,y,seq);
  xx=16'h00f1;yy=16'h0123;status=8'h81;
  wait(seq==2);#1;
  if(x!==xx || y!==yy)$fatal(1,"stale high coordinate byte");
  status=8'h82;wait(seq==3);#1;
  if(down||points!=2)$fatal(1,"multi-touch did not cancel");
  status=8'h80;wait(seq==4);#1;
  if(down||points!=0)$fatal(1,"release failed");
  if(clears!=4)$fatal(1,"snapshot published before clear");
  $display("PASS GT911 ID/config/full16bit/move/multi/release/clear/clock-stretch");$finish;
 end
 initial begin #5000000;$fatal(1,"GT911 test timed out state=%d errors=%d",dut.state,errors);end
endmodule
