`timescale 1ns/1ps
module tb_registers;
 reg clk=0;always #5 clk=~clk;reg rstn=0;
 reg [11:0] awaddr=0,araddr=0;reg awvalid=0,wvalid=0,arvalid=0;
 reg [31:0] wd=0;reg [3:0] strobe=15;
 wire awready,wready,bvalid,arready,rvalid;wire [1:0] bresp,rresp;wire [31:0] rd;
 wire [31:0] base,id;reg [31:0] ackbase=0,ackid=0;tri1 scl,sda,ip;wire tp_rst;
 touch_viewer_ctrl dut(.clk(clk),.rst_n(rstn),.s_axi_awaddr(awaddr),.s_axi_awvalid(awvalid),
  .s_axi_awready(awready),.s_axi_wdata(wd),.s_axi_wstrb(strobe),.s_axi_wvalid(wvalid),
  .s_axi_wready(wready),.s_axi_bresp(bresp),.s_axi_bvalid(bvalid),.s_axi_bready(1'b1),
  .s_axi_araddr(araddr),.s_axi_arvalid(arvalid),.s_axi_arready(arready),
  .s_axi_rdata(rd),.s_axi_rresp(rresp),.s_axi_rvalid(rvalid),.s_axi_rready(1'b1),
  .tp_scl(scl),.tp_sda(sda),.tp_rst(tp_rst),.tp_int(ip),
  .viewer_req_base(base),.viewer_req_id(id),.viewer_ack_base(ackbase),
  .viewer_ack_id(ackid),.viewer_fault(1'b0));
 task write_reg(input [11:0] a,input [31:0] d,input [3:0] ss,input [1:0] expected);
  begin
   @(negedge clk);awaddr=a;awvalid=1;
   do @(posedge clk);while(!awready);
   @(negedge clk);awvalid=0;awaddr=12'hfff;
   repeat(4)@(posedge clk); // Deliberately separate AW from W.
   @(negedge clk);wd=d;strobe=ss;wvalid=1;
   do @(posedge clk);while(!wready);
   @(negedge clk);wvalid=0;wd=32'hdeadbeef;
   wait(bvalid);#1;if(bresp!==expected)$fatal(1,"write response a=%h got=%d",a,bresp);
   @(negedge clk);repeat(2)@(posedge clk);
  end
 endtask
 task read_reg(input [11:0] a,input [31:0] expected);
  begin
   @(negedge clk);araddr=a;arvalid=1;
   do @(posedge clk);while(!arready);
   @(negedge clk);arvalid=0;
   wait(rvalid);#1;if(rd!==expected||rresp!=0)$fatal(1,"read mismatch %h got=%h",a,rd);
   @(negedge clk);repeat(2)@(posedge clk);
  end
 endtask
 initial begin
  #30;rstn=1;read_reg(0,32'h54563837);read_reg(4,87);
  write_reg(12'h020,32'h10000000,15,0);write_reg(12'h024,1,15,0);
  if(id!=1||base!=32'h10000000)$fatal(1,"commit payload wrong");
  write_reg(12'h020,32'h11000000,15,0);write_reg(12'h024,1,15,2);
  if(id!=1||base!=32'h10000000)$fatal(1,"busy request changed held payload");
  @(negedge clk);ackbase=base;ackid=id;repeat(8)@(posedge clk);
  read_reg(12'h028,1);read_reg(12'h02c,32'h10000000);
  write_reg(12'h020,32'h01800000,15,0);write_reg(12'h024,1,15,2);
  write_reg(12'h020,32'h20000000,15,0);write_reg(12'h024,1,15,2);
  write_reg(12'h020,32'h11000001,15,0);write_reg(12'h024,1,15,2);
  write_reg(12'h020,0,1,2);write_reg(12'h020,0,15,0);write_reg(12'h024,1,15,0);
  if(id!=2||base!=0)$fatal(1,"return-camera request wrong");
  $display("PASS AXI separated AW/W, magic, ownership, busy, memory bounds and WSTRB");$finish;
 end
 initial begin #100000;$fatal(1,"register TB timeout");end
endmodule
