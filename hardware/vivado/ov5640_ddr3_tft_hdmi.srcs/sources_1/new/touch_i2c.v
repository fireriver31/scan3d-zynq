// One GT911 byte transaction: 16-bit register, repeated START, open-drain bus.
// An absent/stretched/stuck bus always times out. Camera SCCB is untouched.
module touch_i2c #(
    parameter CLK_HZ=50000000, parameter BUS_HZ=100000
)(input clk, input rst_n, input go, input read_en,
  input [6:0] device, input [15:0] address, input [7:0] wr_data,
  output reg [7:0] rd_data, output reg done, output reg failed,
  inout scl, inout sda,
  output wire [31:0] diag_live, output reg [31:0] diag_failure,
  output reg [31:0] diag_ack_count, output reg [31:0] diag_scl_edges);
  localparam Q=(CLK_HZ/(BUS_HZ*4) < 2) ? 2 : CLK_HZ/(BUS_HZ*4);
  localparam IDLE=0, START=1, START_HOLD=2, TX_LOW=3, TX_RISE=4,
    TX_HIGH=5, TX_FALL=6, ACK_LOW=7, ACK_RISE=8, ACK_HIGH=9,
    ACK_FALL=10, RESTART_LOW=11, RESTART_RISE=12, RESTART_HIGH=13,
    RX_LOW=14, RX_RISE=15, RX_HIGH=16, RX_FALL=17,
    NACK_LOW=18, NACK_RISE=19, NACK_HIGH=20, NACK_FALL=21,
    STOP_LOW=22, STOP_RISE=23, STOP_HIGH=24, STOP_RELEASE=25;
  reg [4:0] state;
  reg scl_low, sda_low, reading, nack;
  reg [6:0] dev;
  reg [15:0] addr;
  reg [7:0] tx, rx, data;
  reg [2:0] bit_no;
  reg [1:0] byte_no;
  integer div, watchdog;
  (* ASYNC_REG="TRUE" *) reg [1:0] scl_sync, sda_sync;
  assign diag_live={14'd0,failed,done,nack,reading,sda_low,scl_low,
    sda_sync[1],scl_sync[1],bit_no,byte_no,state};
  reg diag_old_scl;
  assign scl = scl_low ? 1'b0 : 1'bz;
  assign sda = sda_low ? 1'b0 : 1'bz;
  always @(posedge clk) begin
    scl_sync <= {scl_sync[0], scl}; sda_sync <= {sda_sync[0], sda};
  end
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state<=IDLE; scl_low<=0; sda_low<=0; done<=0; failed<=0;
      div<=0; watchdog<=0; rd_data<=0; tx<=0; rx<=0; byte_no<=0;
      bit_no<=7; reading<=0; dev<=0; addr<=0; data<=0; nack<=0;
      diag_failure<=0;diag_ack_count<=0;diag_scl_edges<=0;diag_old_scl<=1;
    end else begin
      done<=0;
      diag_old_scl<=scl_sync[1];
      if(scl_sync[1]&&!diag_old_scl)diag_scl_edges<=diag_scl_edges+1;
      if (state!=IDLE) watchdog<=watchdog+1; else watchdog<=0;
      if (state!=IDLE && watchdog>=CLK_HZ/50) begin
        state<=IDLE; scl_low<=0; sda_low<=0; failed<=1; done<=1;
        diag_failure<={2'd0,dev,reading,sda_low,scl_low,sda_sync[1],scl_sync[1],
          bit_no,byte_no,state,8'd1};
      end else if (state==IDLE) begin
        div<=0; scl_low<=0; sda_low<=0;
        if (go) begin
          reading<=read_en; dev<=device; addr<=address; data<=wr_data;
          byte_no<=0; tx<={device,1'b0}; bit_no<=7; rx<=0;
          failed<=0; nack<=0; state<=START;
        end
      end else if (div<Q-1) div<=div+1;
      else begin
        div<=0;
        case(state)
          START: if(scl_sync[1] && sda_sync[1]) begin sda_low<=1;state<=START_HOLD;end
          START_HOLD: begin scl_low<=1;state<=TX_LOW;end
          TX_LOW: begin sda_low<=~tx[bit_no];state<=TX_RISE;end
          TX_RISE: begin scl_low<=0;if(scl_sync[1])state<=TX_HIGH;end
          TX_HIGH: state<=TX_FALL;
          TX_FALL: begin
            scl_low<=1;
            if(bit_no==0)state<=ACK_LOW;else begin bit_no<=bit_no-1;state<=TX_LOW;end
          end
          ACK_LOW: begin sda_low<=0;state<=ACK_RISE;end
          ACK_RISE: begin scl_low<=0;if(scl_sync[1])state<=ACK_HIGH;end
          ACK_HIGH: begin nack<=sda_sync[1];state<=ACK_FALL;end
          ACK_FALL: begin
            scl_low<=1;bit_no<=7;
            if(!nack)diag_ack_count<=diag_ack_count+1;
            if(nack)begin
              failed<=1;state<=STOP_LOW;
              diag_failure<={2'd0,dev,reading,sda_low,scl_low,sda_sync[1],scl_sync[1],
                bit_no,byte_no,state,(8'd2+{6'd0,byte_no})};
            end
            else case(byte_no)
              0:begin byte_no<=1;tx<=addr[15:8];state<=TX_LOW;end
              1:begin byte_no<=2;tx<=addr[7:0];state<=TX_LOW;end
              2:begin
                byte_no<=3;
                if(reading)state<=RESTART_LOW;else begin tx<=data;state<=TX_LOW;end
              end
              3:if(reading)state<=RX_LOW;else state<=STOP_LOW;
            endcase
          end
          RESTART_LOW:begin sda_low<=0;state<=RESTART_RISE;end
          RESTART_RISE:begin scl_low<=0;if(scl_sync[1])state<=RESTART_HIGH;end
          RESTART_HIGH:begin sda_low<=1;tx<={dev,1'b1};state<=START_HOLD;end
          RX_LOW:begin sda_low<=0;state<=RX_RISE;end
          RX_RISE:begin scl_low<=0;if(scl_sync[1])state<=RX_HIGH;end
          RX_HIGH:begin rx<={rx[6:0],sda_sync[1]};state<=RX_FALL;end
          RX_FALL:begin
            scl_low<=1;
            if(bit_no==0)begin rd_data<=rx;state<=NACK_LOW;end
            else begin bit_no<=bit_no-1;state<=RX_LOW;end
          end
          NACK_LOW:begin sda_low<=0;state<=NACK_RISE;end
          NACK_RISE:begin scl_low<=0;if(scl_sync[1])state<=NACK_HIGH;end
          NACK_HIGH:state<=NACK_FALL;
          NACK_FALL:begin scl_low<=1;state<=STOP_LOW;end
          STOP_LOW:begin sda_low<=1;state<=STOP_RISE;end
          STOP_RISE:begin scl_low<=0;if(scl_sync[1])state<=STOP_HIGH;end
          STOP_HIGH:begin sda_low<=0;state<=STOP_RELEASE;end
          STOP_RELEASE:begin state<=IDLE;done<=1;end
          default:state<=IDLE;
        endcase
      end
    end
  end
endmodule
