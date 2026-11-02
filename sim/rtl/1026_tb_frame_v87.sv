`timescale 1ns/1ps
module tb_frame;
 reg clk=0;always #5 clk=~clk;
 reg reset=1,frame=0,blank=1;reg [31:0] reqbase=0,reqid=0;
 wire [31:0] ackbase,ackid;wire fault,frst,fw;
 wire [63:0] fd;wire [3:0] arid;wire [31:0] araddr;
 wire [7:0] arlen;wire [2:0] arsize;wire [1:0] arburst;wire [0:0] arlock;
 wire [3:0] arcache,arqos,arregion;wire [2:0] arprot;wire arvalid,rready;
 reg arready=0,rvalid=0,rlast=0;reg [63:0] rdata=0;
 integer beats=-1,delay=0,reads=0;reg outstanding=0;reg [31:0] captured;
 viewer_frame_reader #(.CAMERA_BASE(32'h1000),.FRAME_BYTES(256)) dut(
  .clk(clk),.reset(reset),.frame_start(frame),.vertical_blank(blank),.req_base(reqbase),.req_id(reqid),
  .ack_base(ackbase),.ack_id(ackid),.fault(fault),.fifo_reset(frst),
  .fifo_wrreq(fw),.fifo_wrdata(fd),.fifo_alfull(1'b0),.fifo_wr_cnt(6'd0),.fifo_rst_busy(frst),
  .m_axi_arid(arid),.m_axi_araddr(araddr),.m_axi_arlen(arlen),.m_axi_arsize(arsize),
  .m_axi_arburst(arburst),.m_axi_arlock(arlock),.m_axi_arcache(arcache),.m_axi_arprot(arprot),
  .m_axi_arqos(arqos),.m_axi_arregion(arregion),.m_axi_arvalid(arvalid),.m_axi_arready(arready),
  .m_axi_rid(4'd0),.m_axi_rdata(rdata),.m_axi_rresp(2'd0),.m_axi_rlast(rlast),
  .m_axi_rvalid(rvalid),.m_axi_rready(rready));
 always @(posedge clk)begin
  if(!reset)begin
   if(arvalid && arready)begin
    if(outstanding)$fatal(1,"more than one outstanding read");
    captured<=araddr;outstanding<=1;beats<=0;delay<=3;reads<=reads+1;
   end
   if(outstanding && !rvalid)begin
    if(delay>0)delay<=delay-1;
    else begin rvalid<=1;rlast<=beats==15;rdata<={captured,beats[31:0]};end
   end
   if(rvalid && rready)begin
    rvalid<=0;
    if(rlast)begin outstanding<=0;beats<=-1;end else beats<=beats+1;
   end
   if(frst && outstanding)$fatal(1,"FIFO reset with burst in flight");
   if(ackid==1 && outstanding && captured<32'h2000)$fatal(1,"released old buffer before drain");
  end
 end
 initial begin
  #30;reset=0;
  wait(arvalid);captured=araddr;repeat(10)begin @(posedge clk);if(araddr!=captured||!arvalid)$fatal(1,"unstable AR under stall");end
  @(negedge clk);arready=1;
  wait(beats==4);
  @(negedge clk);reqbase=32'h2000;reqid=1;
  repeat(8)@(posedge clk);
  if(ackid!=0)$fatal(1,"switched outside vertical blank");
  @(negedge clk);frame=1;repeat(5)@(posedge clk);@(negedge clk);frame=0;blank=0;
  repeat(30)@(posedge clk);
  if(ackid!=0)$fatal(1,"late burst caused mid-visible-frame ownership change");
  @(negedge clk);blank=1;
  wait(ackid==1);#1;if(ackbase!=32'h2000)$fatal(1,"wrong acknowledged base");
  wait(reads>=3);repeat(200)@(posedge clk);
  if(fault)$fatal(1,"unexpected AXI fault");
  @(negedge clk);reqbase=0;reqid=2;repeat(8)@(posedge clk);
  @(negedge clk);frame=1;repeat(5)@(posedge clk);@(negedge clk);frame=0;
  wait(ackid==2);#1;if(ackbase!=0)$fatal(1,"camera return failed");
  $display("PASS framebuffer AR stall/drain/frame flip/return-camera");$finish;
 end
 initial begin #100000;$fatal(1,"frame reader timed out");end
endmodule
