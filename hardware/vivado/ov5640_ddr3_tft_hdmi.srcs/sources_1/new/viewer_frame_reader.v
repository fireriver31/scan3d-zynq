// Shared camera/viewer DDR display reader. Camera write channel is independent.
// Exactly one outstanding burst; drain before FIFO reset or frame ownership ACK.
module viewer_frame_reader #(
 parameter CAMERA_BASE=32'h01800000, parameter FRAME_BYTES=768000,
 parameter AXI_DATA_WIDTH=64,parameter AXI_ADDR_WIDTH=32,
 parameter AXI_ID_WIDTH=4,parameter AXI_ID=0,parameter AXI_BURST_LEN=15,
 parameter FIFO_ADDR_WIDTH=6
)(input clk,input reset,input frame_start,input vertical_blank,input [31:0] req_base,input [31:0] req_id,
 output reg [31:0] ack_base,output reg [31:0] ack_id,output reg fault,
 output reg fifo_reset,output reg fifo_wrreq,output reg [AXI_DATA_WIDTH-1:0] fifo_wrdata,
 input fifo_alfull,input [FIFO_ADDR_WIDTH-1:0] fifo_wr_cnt,input fifo_rst_busy,
 output [AXI_ID_WIDTH-1:0] m_axi_arid,output reg [AXI_ADDR_WIDTH-1:0] m_axi_araddr,
 output [7:0] m_axi_arlen,output [2:0] m_axi_arsize,output [1:0] m_axi_arburst,
 output [0:0] m_axi_arlock,output [3:0] m_axi_arcache,output [2:0] m_axi_arprot,
 output [3:0] m_axi_arqos,output [3:0] m_axi_arregion,output reg m_axi_arvalid,
 input m_axi_arready,input [AXI_ID_WIDTH-1:0] m_axi_rid,
 input [AXI_DATA_WIDTH-1:0] m_axi_rdata,input [1:0] m_axi_rresp,
 input m_axi_rlast,input m_axi_rvalid,output m_axi_rready);
 localparam BURST_BYTES=(AXI_BURST_LEN+1)*(AXI_DATA_WIDTH/8);
 localparam IDLE=0,ADDRESS=1,RESPONSE=2,FLUSH=3,WAIT_FIFO=4;
 reg [2:0] state;
 (* ASYNC_REG="TRUE" *) reg [2:0] frame_sync;
 (* ASYNC_REG="TRUE" *) reg [2:0] blank_sync;
 (* ASYNC_REG="TRUE" *) reg [2:0] req_sync;
 reg restart;
 reg [6:0] flush_count;
 reg [31:0] offset,active;
 reg [7:0] beats;
 assign m_axi_arid=AXI_ID;
 assign m_axi_arlen=AXI_BURST_LEN;
 assign m_axi_arsize=$clog2(AXI_DATA_WIDTH/8);
 assign m_axi_arburst=2'b01;assign m_axi_arlock=0;
 assign m_axi_arcache=0;assign m_axi_arprot=0;
 assign m_axi_arqos=0;assign m_axi_arregion=0;
 assign m_axi_rready=(state==RESPONSE) && (!fifo_alfull || restart);
 always @(posedge clk or posedge reset)begin
  if(reset)begin
   frame_sync<=0;blank_sync<=0;req_sync<=0;ack_base<=0;ack_id<=0;
   active<=CAMERA_BASE;offset<=0;state<=FLUSH;restart<=0;
   fifo_reset<=1;flush_count<=0;fifo_wrreq<=0;fifo_wrdata<=0;
   fault<=0;m_axi_arvalid<=0;m_axi_araddr<=CAMERA_BASE;beats<=0;
  end else begin
   frame_sync<={frame_sync[1:0],frame_start};
   blank_sync<={blank_sync[1:0],vertical_blank};
   req_sync<={req_sync[1:0],req_id[0]};
   fifo_wrreq<=0;
   if(frame_sync[1] && !frame_sync[2])restart<=1;
   case(state)
    IDLE:begin
     if(restart && blank_sync[2])begin
      // All previous requests and FIFO pipeline writes have retired here.
      if(req_sync[2]!=ack_id[0])begin
       active<=(req_base==0)?CAMERA_BASE:req_base;
       ack_base<=req_base;ack_id<=req_id;
      end
      offset<=0;restart<=0;fifo_reset<=1;flush_count<=0;state<=FLUSH;
     end else if(!restart && offset<FRAME_BYTES && !fifo_rst_busy && !fifo_alfull &&
          fifo_wr_cnt<((1<<FIFO_ADDR_WIDTH)-(AXI_BURST_LEN+1)-2))begin
      m_axi_araddr<=active+offset;m_axi_arvalid<=1;state<=ADDRESS;
     end
    end
    ADDRESS:if(m_axi_arvalid && m_axi_arready)begin
     m_axi_arvalid<=0;state<=RESPONSE;beats<=0;
    end
    RESPONSE:if(m_axi_rvalid && m_axi_rready)begin
     if(m_axi_rresp!=0 || m_axi_rid!=AXI_ID)fault<=1;
     if(!restart)begin fifo_wrreq<=1;fifo_wrdata<=m_axi_rdata;end
     beats<=beats+1;
     if(m_axi_rlast)begin
      if(beats!=AXI_BURST_LEN)fault<=1;
      offset<=offset+BURST_BYTES;state<=IDLE;
     end
    end
    FLUSH:begin
     fifo_reset<=1;
     if(flush_count==63)begin fifo_reset<=0;state<=WAIT_FIFO;end
     else flush_count<=flush_count+1;
    end
    WAIT_FIFO:if(!fifo_rst_busy)state<=IDLE;
    default:state<=IDLE;
   endcase
  end
 end
endmodule
