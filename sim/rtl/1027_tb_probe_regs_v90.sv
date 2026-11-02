`timescale 1ns/1ps
module tb_probe_regs_v90;
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
  read_reg(12'h034,32'h54503930);
  write_reg(12'h034,32'h0,15,2);read_reg(12'h034,32'h54503930);
  force dut.diag_reader_live=32'ha0000000;
  read_reg(12'h038,32'ha0000000);
  release dut.diag_reader_live;
  force dut.diag_bus_live=32'ha0010101;
  read_reg(12'h03c,32'ha0010101);
  release dut.diag_bus_live;
  force dut.diag_last_reader=32'ha0020202;
  read_reg(12'h040,32'ha0020202);
  release dut.diag_last_reader;
  force dut.diag_last_bus=32'ha0030303;
  read_reg(12'h044,32'ha0030303);
  release dut.diag_last_bus;
  force dut.diag_last_id=32'ha0040404;
  read_reg(12'h048,32'ha0040404);
  release dut.diag_last_id;
  force dut.diag_ack_count=32'ha0050505;
  read_reg(12'h04c,32'ha0050505);
  release dut.diag_ack_count;
  force dut.diag_scl_edges=32'ha0060606;
  read_reg(12'h050,32'ha0060606);
  release dut.diag_scl_edges;
  force dut.diag_attempts_5d=32'ha0070707;
  read_reg(12'h054,32'ha0070707);
  release dut.diag_attempts_5d;
  force dut.diag_attempts_14=32'ha0080808;
  read_reg(12'h058,32'ha0080808);
  release dut.diag_attempts_14;
  force dut.diag_error_seq=32'ha0090909;
  read_reg(12'h05c,32'ha0090909);
  release dut.diag_error_seq;
  force dut.diag_5d_reader=32'ha00a0a0a;
  read_reg(12'h060,32'ha00a0a0a);
  release dut.diag_5d_reader;
  force dut.diag_5d_bus=32'ha00b0b0b;
  read_reg(12'h064,32'ha00b0b0b);
  release dut.diag_5d_bus;
  force dut.diag_5d_id=32'ha00c0c0c;
  read_reg(12'h068,32'ha00c0c0c);
  release dut.diag_5d_id;
  force dut.diag_14_reader=32'ha00d0d0d;
  read_reg(12'h06c,32'ha00d0d0d);
  release dut.diag_14_reader;
  force dut.diag_14_bus=32'ha00e0e0e;
  read_reg(12'h070,32'ha00e0e0e);
  release dut.diag_14_bus;
  force dut.diag_14_id=32'ha00f0f0f;
  read_reg(12'h074,32'ha00f0f0f);
  release dut.diag_14_id;
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
  $display("PASS 16 new diagnostic registers, read-only tag, legacy ABI and framebuffer ownership");$finish;
 end
 initial begin #100000;$fatal(1,"register TB timeout");end
endmodule
