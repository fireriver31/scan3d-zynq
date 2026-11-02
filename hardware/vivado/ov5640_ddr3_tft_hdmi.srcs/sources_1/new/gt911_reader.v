// GT911 standard 8-byte contact: ID=814f X=8150/51 Y=8152/53.
// Publish only after all coordinates AND status-clear succeeded. No stale YH.
module gt911_reader #(parameter CLK_HZ=50000000, parameter BUS_HZ=100000,
                     parameter TIME_SCALE=1)
(input clk,input rst_n,inout tp_scl,inout tp_sda,output reg tp_rst,
 inout tp_int,output reg online,output reg down,output reg [3:0] points,
 output reg [15:0] x,output reg [15:0] y,output reg [31:0] touch_seq,
 output reg [23:0] product,output reg [15:0] max_x,output reg [15:0] max_y,
 output reg [31:0] errors,output reg address_sel,
 output wire [31:0] diag_reader_live,output wire [31:0] diag_bus_live,
 output reg [31:0] diag_last_reader,diag_last_bus,diag_last_id,
 output reg [31:0] diag_5d_reader,diag_5d_bus,diag_5d_id,
 output reg [31:0] diag_14_reader,diag_14_bus,diag_14_id,
 output wire [31:0] diag_ack_count,diag_scl_edges,
 output reg [31:0] diag_attempts_5d,diag_attempts_14,
 output reg [31:0] diag_error_seq);
 localparam MS=(CLK_HZ/1000/TIME_SCALE < 1) ? 1: CLK_HZ/1000/TIME_SCALE;
 localparam RESET=0, SELECT=1, BOOT=2, ID_GO=3, ID_WAIT=4,
   CFG_GO=5, CFG_WAIT=6, POLL=7, STATUS_GO=8, STATUS_WAIT=9,
   XY_GO=10, XY_WAIT=11, CLEAR_GO=12, CLEAR_WAIT=13, RETRY=14;
 reg [4:0] state;
 reg int_drive, int_value, go, read_en;
 reg [15:0] reg_addr;
 wire [7:0] rd;
 wire done, failed;
 reg [31:0] timer;
 reg [2:0] index;
 reg [7:0] xl,xh,yl,yh, cfg0,cfg1,cfg2,cfg3;
 reg [3:0] pending_points;
 wire [31:0] bus_failure;
 reg [23:0] attempt_id;
 reg [2:0] id_valid;
 reg [1:0] int_sync;
 assign diag_reader_live={reg_addr,5'd0,int_sync[1],tp_rst,int_drive,int_value,address_sel,online,state};
 always @(posedge clk)int_sync<={int_sync[0],tp_int};
 wire diag_bus_error=done&&failed;
 wire diag_bad_id=done&&!failed&&state==ID_WAIT&&index==2&&
   !(product[15:0]==16'h3139 && rd==8'h31);
 wire [31:0] error_reader={reg_addr,2'd0,address_sel,index,state,(diag_bad_id?5'd2:5'd1)};
 wire [31:0] error_bus=diag_bad_id?32'd0:bus_failure;
 wire [31:0] error_id=diag_bad_id?{5'd0,3'b111,rd,attempt_id[15:0]}:{5'd0,id_valid,attempt_id};
 always @(posedge clk or negedge rst_n)begin
   if(!rst_n)begin
     attempt_id<=0;id_valid<=0;diag_last_reader<=0;diag_last_bus<=0;
     diag_last_id<=0;diag_attempts_5d<=0;diag_attempts_14<=0;diag_error_seq<=0;
     diag_5d_reader<=0;diag_5d_bus<=0;diag_5d_id<=0;
     diag_14_reader<=0;diag_14_bus<=0;diag_14_id<=0;
   end else begin
     if(state==BOOT&&timer==0)begin
       attempt_id<=0;id_valid<=0;
       if(address_sel)diag_attempts_14<=diag_attempts_14+1;
       else diag_attempts_5d<=diag_attempts_5d+1;
     end
     if(state==ID_WAIT&&done&&!failed)begin
       case(index)0:attempt_id[7:0]<=rd;1:attempt_id[15:8]<=rd;2:attempt_id[23:16]<=rd;endcase
       id_valid[index]<=1;
     end
     if((diag_bus_error&&(state==ID_WAIT||state==CFG_WAIT||state==STATUS_WAIT||state==XY_WAIT||state==CLEAR_WAIT))||diag_bad_id)begin
       diag_error_seq<=diag_error_seq+1;
       diag_last_reader<=error_reader;diag_last_bus<=error_bus;diag_last_id<=error_id;
       if(address_sel)begin
         diag_14_reader<=error_reader;diag_14_bus<=error_bus;diag_14_id<=error_id;
       end else begin
         diag_5d_reader<=error_reader;diag_5d_bus<=error_bus;diag_5d_id<=error_id;
       end
     end
   end
 end
 assign tp_int = int_drive ? int_value : 1'bz;
 touch_i2c #(.CLK_HZ(CLK_HZ),.BUS_HZ(BUS_HZ)) bus(
  .clk(clk),.rst_n(rst_n),.go(go),.read_en(read_en),
  .device(address_sel ? 7'h14:7'h5d),.address(reg_addr),.wr_data(8'd0),
  .rd_data(rd),.done(done),.failed(failed),.scl(tp_scl),.sda(tp_sda),
  .diag_live(diag_bus_live),.diag_failure(bus_failure),
  .diag_ack_count(diag_ack_count),.diag_scl_edges(diag_scl_edges));
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n)begin
    state<=RESET;timer<=20*MS;tp_rst<=0;int_drive<=1;int_value<=0;
    online<=0;down<=0;points<=0;x<=0;y<=0;touch_seq<=0;product<=0;
    max_x<=0;max_y<=0;errors<=0;address_sel<=0;go<=0;read_en<=1;
    index<=0;reg_addr<=0;xl<=0;xh<=0;yl<=0;yh<=0;
    cfg0<=0;cfg1<=0;cfg2<=0;cfg3<=0;pending_points<=0;
  end else begin
   go<=0;
   if(timer!=0)timer<=timer-1;
   case(state)
    RESET:begin
      tp_rst<=0;int_drive<=1;int_value<=address_sel;online<=0;down<=0;points<=0;
      if(timer==0)begin tp_rst<=1;timer<=6*MS;state<=SELECT;end
    end
    SELECT:if(timer==0)begin int_value<=0;timer<=50*MS;state<=BOOT;end
    BOOT:if(timer==0)begin int_drive<=0;index<=0;product<=0;state<=ID_GO;end
    ID_GO:begin read_en<=1;reg_addr<=16'h8140+index;go<=1;state<=ID_WAIT;end
    ID_WAIT:if(done)begin
      if(failed)begin errors<=errors+1;state<=RETRY;end
      else begin
        case(index)0:product[7:0]<=rd;1:product[15:8]<=rd;2:product[23:16]<=rd;endcase
        if(index==2)begin
          if(product[15:0]==16'h3139 && rd==8'h31)begin index<=0;state<=CFG_GO;end
          else begin errors<=errors+1;state<=RETRY;end
        end else begin index<=index+1;state<=ID_GO;end
      end
    end
    CFG_GO:begin read_en<=1;reg_addr<=16'h8048+index;go<=1;state<=CFG_WAIT;end
    CFG_WAIT:if(done)begin
      if(failed)begin errors<=errors+1;state<=RETRY;end
      else begin
        case(index)0:cfg0<=rd;1:cfg1<=rd;2:cfg2<=rd;3:cfg3<=rd;endcase
        if(index==3)begin
          max_x<={cfg1,cfg0};max_y<={rd,cfg2};online<=1;timer<=10*MS;state<=POLL;
        end else begin index<=index+1;state<=CFG_GO;end
      end
    end
    POLL:if(timer==0)state<=STATUS_GO;
    STATUS_GO:begin read_en<=1;reg_addr<=16'h814e;go<=1;state<=STATUS_WAIT;end
    STATUS_WAIT:if(done)begin
      if(failed)begin errors<=errors+1;state<=RETRY;end
      else if(!rd[7])begin timer<=10*MS;state<=POLL;end
      else begin
        pending_points<=rd[3:0];index<=0;
        if(rd[3:0]==1)state<=XY_GO;else state<=CLEAR_GO;
      end
    end
    XY_GO:begin read_en<=1;reg_addr<=16'h8150+index;go<=1;state<=XY_WAIT;end
    XY_WAIT:if(done)begin
      if(failed)begin errors<=errors+1;state<=RETRY;end
      else begin
        case(index)0:xl<=rd;1:xh<=rd;2:yl<=rd;3:yh<=rd;endcase
        if(index==3)state<=CLEAR_GO;else begin index<=index+1;state<=XY_GO;end
      end
    end
    CLEAR_GO:begin read_en<=0;reg_addr<=16'h814e;go<=1;state<=CLEAR_WAIT;end
    CLEAR_WAIT:if(done)begin
      if(failed)begin errors<=errors+1;state<=RETRY;end
      else begin
        points<=pending_points;touch_seq<=touch_seq+1;
        // Multi-touch cancels drag; never switch silently between fingers.
        down<=pending_points==1;
        if(pending_points==1)begin x<={xh,xl};y<={yh,yl};end
        timer<=10*MS;state<=POLL;
      end
    end
    RETRY:begin
      online<=0;down<=0;points<=0;touch_seq<=touch_seq+1;
      address_sel<=~address_sel;timer<=20*MS;state<=RESET;
    end
    default:state<=RETRY;
   endcase
  end
 end
endmodule
