// Separate AXI-Lite peripheral. Never overlaps reconstruction coefficient RAM.
module touch_viewer_ctrl #(
 parameter CLK_HZ=50000000, parameter BUS_HZ=100000
)(
 (* X_INTERFACE_PARAMETER="ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET rst_n, FREQ_HZ 50000000" *)
 input clk,input rst_n,
 input [11:0] s_axi_awaddr,input s_axi_awvalid,output reg s_axi_awready,
 input [31:0] s_axi_wdata,input [3:0] s_axi_wstrb,input s_axi_wvalid,
 output reg s_axi_wready,output reg [1:0] s_axi_bresp,output reg s_axi_bvalid,
 input s_axi_bready,input [11:0] s_axi_araddr,input s_axi_arvalid,
 output reg s_axi_arready,output reg [31:0] s_axi_rdata,
 output reg [1:0] s_axi_rresp,output reg s_axi_rvalid,input s_axi_rready,
 inout tp_scl,inout tp_sda,output tp_rst,inout tp_int,
 output reg [31:0] viewer_req_base,output reg [31:0] viewer_req_id,
 input [31:0] viewer_ack_base,input [31:0] viewer_ack_id,
 input viewer_fault,
 input [191:0] display_diag,input display_diag_toggle
);
 wire online,down,addr_sel;
 wire [3:0] points;
 wire [15:0] x,y,max_x,max_y;
 wire [31:0] seq,errors;
 wire [23:0] product;
 wire [31:0] diag_reader_live,diag_bus_live,diag_last_reader,diag_last_bus,diag_last_id;
 wire [31:0] diag_5d_reader,diag_5d_bus,diag_5d_id,diag_14_reader,diag_14_bus,diag_14_id;
 wire [31:0] diag_ack_count,diag_scl_edges,diag_attempts_5d,diag_attempts_14,diag_error_seq;
 gt911_reader #(.CLK_HZ(CLK_HZ),.BUS_HZ(BUS_HZ)) touch(
  .clk(clk),.rst_n(rst_n),.tp_scl(tp_scl),.tp_sda(tp_sda),.tp_rst(tp_rst),
  .tp_int(tp_int),.online(online),.down(down),.points(points),.x(x),.y(y),
  .touch_seq(seq),.product(product),.max_x(max_x),.max_y(max_y),
  .errors(errors),.address_sel(addr_sel),
  .diag_reader_live(diag_reader_live),.diag_bus_live(diag_bus_live),
  .diag_last_reader(diag_last_reader),.diag_last_bus(diag_last_bus),.diag_last_id(diag_last_id),
  .diag_5d_reader(diag_5d_reader),.diag_5d_bus(diag_5d_bus),.diag_5d_id(diag_5d_id),
  .diag_14_reader(diag_14_reader),.diag_14_bus(diag_14_bus),.diag_14_id(diag_14_id),
  .diag_ack_count(diag_ack_count),.diag_scl_edges(diag_scl_edges),
  .diag_attempts_5d(diag_attempts_5d),.diag_attempts_14(diag_attempts_14),.diag_error_seq(diag_error_seq));
 // Bundled-data handshake: ACK/base held at 200MHz until next request. Request
 // ID is synchronized; sample base only after synchronized ID is stable.
 (* ASYNC_REG="TRUE" *) reg [2:0] ack_sync;
 (* ASYNC_REG="TRUE" *) reg [1:0] fault_sync;
 reg [31:0] ack_seen,active_base;
 reg aw_pending,w_pending;
 reg [11:0] awaddr;
 reg [31:0] wdata,shadow_base;
 reg [3:0] wstrb;
 reg [31:0] rd;
 (* ASYNC_REG="TRUE" *) reg [2:0] display_toggle_sync;
 reg display_seen;
 reg [191:0] display_latched;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin display_toggle_sync<=0;display_seen<=0;display_latched<=0;end
  else begin
   display_toggle_sync<={display_toggle_sync[1:0],display_diag_toggle};
   if(display_toggle_sync[2]!=display_seen)begin
    display_latched<=display_diag;display_seen<=display_toggle_sync[2];
   end
  end
 end
 wire execute=aw_pending && w_pending && !s_axi_bvalid;
 wire pending=(viewer_req_id!=ack_seen);
 wire base_valid=(shadow_base==0) || ((shadow_base[6:0]==0) &&
   (shadow_base>=32'h00100000) && (shadow_base<=32'h1ff44800) &&
   ((shadow_base+32'd768000<=32'h01800000) || (shadow_base>=32'h018bb800)));
 always @(*)begin
  case(s_axi_araddr)
   12'h000:rd=32'h54563837;
   12'h004:rd=87;
   12'h008:rd={22'd0,addr_sel,1'b0,points,online,down};
   12'h00c:rd={y,x};
   12'h010:rd=seq;
   12'h014:rd={8'd0,product};
   12'h018:rd={max_y,max_x};
   12'h01c:rd=errors;
   12'h020:rd=shadow_base;
   12'h024:rd=viewer_req_id;
   12'h028:rd=ack_seen;
   12'h02c:rd=active_base;
   12'h030:rd={30'd0,fault_sync[1],pending};
   12'h034:rd=32'h54503930; // TP90: read-only diagnostic extension, legacy ABI87
   12'h038:rd=diag_reader_live;
   12'h03c:rd=diag_bus_live;
   12'h040:rd=diag_last_reader;
   12'h044:rd=diag_last_bus;
   12'h048:rd=diag_last_id;
   12'h04c:rd=diag_ack_count;
   12'h050:rd=diag_scl_edges;
   12'h054:rd=diag_attempts_5d;
   12'h058:rd=diag_attempts_14;
   12'h05c:rd=diag_error_seq;
   12'h060:rd=diag_5d_reader;
   12'h064:rd=diag_5d_bus;
   12'h068:rd=diag_5d_id;
   12'h06c:rd=diag_14_reader;
   12'h070:rd=diag_14_bus;
   12'h074:rd=diag_14_id;
   12'h078:rd=32'h44533939; // DS99, display diagnostics; touch TP90 unchanged
   12'h07c:rd=display_latched[31:0];
   12'h080:rd=display_latched[63:32];
   12'h084:rd=display_latched[95:64];
   12'h088:rd=display_latched[127:96];
   12'h08c:rd=display_latched[159:128];
   12'h090:rd=display_latched[191:160];
   default:rd=32'hdead0000|s_axi_araddr;
  endcase
 end
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   ack_sync<=0;ack_seen<=0;active_base<=0;fault_sync<=0;
   viewer_req_base<=0;viewer_req_id<=0;shadow_base<=0;
   aw_pending<=0;w_pending<=0;awaddr<=0;wdata<=0;wstrb<=0;
   s_axi_awready<=1;s_axi_wready<=1;s_axi_bvalid<=0;s_axi_bresp<=0;
   s_axi_arready<=1;s_axi_rvalid<=0;s_axi_rresp<=0;s_axi_rdata<=0;
  end else begin
   ack_sync<={ack_sync[1:0],viewer_ack_id[0]};
   fault_sync<={fault_sync[0],viewer_fault};
   if(ack_sync[2]!=ack_seen[0])begin ack_seen<=viewer_ack_id;active_base<=viewer_ack_base;end
   if(s_axi_awready && s_axi_awvalid)begin
    aw_pending<=1;awaddr<=s_axi_awaddr;s_axi_awready<=0;
   end
   if(s_axi_wready && s_axi_wvalid)begin
    w_pending<=1;wdata<=s_axi_wdata;wstrb<=s_axi_wstrb;s_axi_wready<=0;
   end
   if(execute)begin
    s_axi_bvalid<=1;s_axi_bresp<=0;
    if(wstrb!=4'hf)s_axi_bresp<=2'b10;
    else case(awaddr)
     12'h020:shadow_base<=wdata;
     12'h024:begin
      if(wdata!=1 || pending || !base_valid)s_axi_bresp<=2'b10;
      else begin viewer_req_base<=shadow_base;viewer_req_id<=viewer_req_id+1;end
     end
     default:s_axi_bresp<=2'b10;
    endcase
   end
   if(s_axi_bvalid && s_axi_bready)begin
    s_axi_bvalid<=0;s_axi_awready<=1;s_axi_wready<=1;aw_pending<=0;w_pending<=0;
   end
   if(s_axi_arready && s_axi_arvalid)begin
    s_axi_arready<=0;s_axi_rvalid<=1;s_axi_rdata<=rd;s_axi_rresp<=0;
   end
   if(s_axi_rvalid && s_axi_rready)begin s_axi_arready<=1;s_axi_rvalid<=0;end
  end
 end
endmodule
