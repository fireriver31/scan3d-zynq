`timescale 1ns/1ps
// DDR-backed PL segmentation. Each input beat is {reference565, current565}.
// Output packs 16 two-bit decisions, low pixel first: bit0=strong, bit1=possible.
// No morphology, outline shifts, hole filling, classes or target dimensions.
module photo_mask_axis (
    (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 clk CLK" *)
    (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME clk, FREQ_HZ 50000000, ASSOCIATED_BUSIF s_axi:s_axis:m_axis, ASSOCIATED_RESET rst_n" *)
    input wire clk,
    (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 rst_n RST" *)
    (* X_INTERFACE_PARAMETER="POLARITY ACTIVE_LOW" *) input wire rst_n,
    input wire [11:0] s_axi_awaddr, input wire s_axi_awvalid, output wire s_axi_awready,
    input wire [31:0] s_axi_wdata, input wire [3:0] s_axi_wstrb,
    input wire s_axi_wvalid, output wire s_axi_wready,
    output reg [1:0] s_axi_bresp, output reg s_axi_bvalid, input wire s_axi_bready,
    input wire [11:0] s_axi_araddr, input wire s_axi_arvalid, output wire s_axi_arready,
    output reg [31:0] s_axi_rdata, output reg [1:0] s_axi_rresp,
    output reg s_axi_rvalid, input wire s_axi_rready,
    input wire [31:0] s_axis_tdata, input wire [3:0] s_axis_tkeep,
    input wire s_axis_tlast, input wire s_axis_tvalid, output wire s_axis_tready,
    output reg [31:0] m_axis_tdata, output wire [3:0] m_axis_tkeep,
    output reg m_axis_tlast, output reg m_axis_tvalid, input wire m_axis_tready
);
    localparam [31:0] VERSION = 32'h504d1901;
    reg aw_hold, w_hold;
    reg [11:0] aw_address;
    reg [31:0] w_data;
    reg [3:0] w_strobe;
    reg cmd_start, cmd_abort, bad_cfg;
    reg [31:0] n_pixels, high_q16, low_q16;
    reg [9:0] lut_address, lut_loaded;
    reg [5:0] noise_address, noise_loaded;
    (* ram_style="distributed" *) reg [7:0] lut_r [0:255];
    (* ram_style="distributed" *) reg [7:0] lut_g [0:255];
    (* ram_style="distributed" *) reg [7:0] lut_b [0:255];
    (* ram_style="distributed" *) reg [15:0] inv_r [0:15];
    (* ram_style="distributed" *) reg [15:0] inv_g [0:15];
    (* ram_style="distributed" *) reg [15:0] inv_b [0:15];
    reg busy, done;
    reg [31:0] errors, rx_pixels, tx_words, frame_sequence;
    reg [31:0] strong_pixels, possible_pixels;
    assign s_axi_awready = !aw_hold;
    assign s_axi_wready = !w_hold;
    assign s_axi_arready = !s_axi_rvalid || s_axi_rready;
    // Address and data channels are accepted independently.
    always @(posedge clk) begin
        if (!rst_n) begin
            aw_hold<=0; w_hold<=0; s_axi_bvalid<=0; s_axi_bresp<=0;
            s_axi_rvalid<=0; s_axi_rresp<=0; s_axi_rdata<=0;
            cmd_start<=0; cmd_abort<=0; bad_cfg<=0;
            n_pixels<=384000; high_q16<=841351; low_q16<=512164;
            lut_address<=0; noise_address<=0; lut_loaded<=0; noise_loaded<=0;
            aw_address<=0; w_data<=0; w_strobe<=0;
        end else begin
            cmd_start<=0; cmd_abort<=0; bad_cfg<=0;
            if (s_axi_awvalid && s_axi_awready) begin aw_address<=s_axi_awaddr; aw_hold<=1; end
            if (s_axi_wvalid && s_axi_wready) begin w_data<=s_axi_wdata; w_strobe<=s_axi_wstrb; w_hold<=1; end
            if (s_axi_bvalid && s_axi_bready) s_axi_bvalid<=0;
            if (aw_hold && w_hold && !s_axi_bvalid) begin
                aw_hold<=0; w_hold<=0; s_axi_bvalid<=1; s_axi_bresp<=0;
                if (w_strobe!=4'hf) begin s_axi_bresp<=2'b10; bad_cfg<=1; end
                else if (aw_address==12'h004) begin cmd_start<=w_data[0]; cmd_abort<=w_data[1]; end
                else if (busy) begin s_axi_bresp<=2'b10; bad_cfg<=1; end
                else case (aw_address)
                    12'h00c: n_pixels<=w_data;
                    12'h010: high_q16<=w_data;
                    12'h014: low_q16<=w_data;
                    12'h020: begin lut_address<=w_data[9:0]; lut_loaded<=0; end
                    12'h024: begin
                        if (lut_address<768) begin
                            case (lut_address[9:8])
                                0:lut_r[lut_address[7:0]]<=w_data[7:0];
                                1:lut_g[lut_address[7:0]]<=w_data[7:0];
                                2:lut_b[lut_address[7:0]]<=w_data[7:0];
                            endcase
                            lut_address<=lut_address+1'b1;
                            if (lut_loaded<768) lut_loaded<=lut_loaded+1'b1;
                        end else begin s_axi_bresp<=2'b10; bad_cfg<=1; end
                    end
                    12'h028: begin noise_address<=w_data[5:0]; noise_loaded<=0; end
                    12'h02c: begin
                        if (noise_address<48 && w_data[15:0]!=0) begin
                            case (noise_address[5:4])
                                0:inv_r[noise_address[3:0]]<=w_data[15:0];
                                1:inv_g[noise_address[3:0]]<=w_data[15:0];
                                2:inv_b[noise_address[3:0]]<=w_data[15:0];
                            endcase
                            noise_address<=noise_address+1'b1;
                            if (noise_loaded<48) noise_loaded<=noise_loaded+1'b1;
                        end else begin s_axi_bresp<=2'b10; bad_cfg<=1; end
                    end
                    default:begin s_axi_bresp<=2'b10; bad_cfg<=1; end
                endcase
            end
            if (s_axi_rvalid && s_axi_rready) s_axi_rvalid<=0;
            if (s_axi_arvalid && s_axi_arready) begin
                s_axi_rvalid<=1; s_axi_rresp<=0;
                case (s_axi_araddr)
                    12'h000:s_axi_rdata<=VERSION;
                    12'h008:s_axi_rdata<={29'b0,(errors!=0),done,busy};
                    12'h00c:s_axi_rdata<=n_pixels;
                    12'h010:s_axi_rdata<=high_q16;
                    12'h014:s_axi_rdata<=low_q16;
                    12'h018:s_axi_rdata<=tx_words;
                    12'h01c:s_axi_rdata<=rx_pixels;
                    12'h030:s_axi_rdata<=frame_sequence;
                    12'h034:s_axi_rdata<=strong_pixels;
                    12'h038:s_axi_rdata<=possible_pixels;
                    12'h03c:s_axi_rdata<=errors;
                    12'h040:s_axi_rdata<={16'b0,noise_loaded,lut_loaded};
                    default:begin s_axi_rdata<=0; s_axi_rresp<=2'b10; end
                endcase
            end
        end
    end

    wire [7:0] cur_r={s_axis_tdata[15:11],s_axis_tdata[15:13]};
    wire [7:0] cur_g={s_axis_tdata[10:5],s_axis_tdata[10:9]};
    wire [7:0] cur_b={s_axis_tdata[4:0],s_axis_tdata[4:2]};
    wire [7:0] ref_r={s_axis_tdata[31:27],s_axis_tdata[31:29]};
    wire [7:0] ref_g={s_axis_tdata[26:21],s_axis_tdata[26:25]};
    wire [7:0] ref_b={s_axis_tdata[20:16],s_axis_tdata[20:18]};
    reg v0,v1,v2,v3,last0,last1,last2,last3;
    reg [7:0] r0,g0,b0,rr0,rg0,rb0;
    reg [15:0] ir0,ig0,ib0,ir1,ig1,ib1;
    wire [7:0] dr=(r0>=rr0)?r0-rr0:rr0-r0;
    wire [7:0] dg=(g0>=rg0)?g0-rg0:rg0-g0;
    wire [7:0] db=(b0>=rb0)?b0-rb0:rb0-b0;
    reg [15:0] sr1,sg1,sb1;
    reg [31:0] pr2,pg2,pb2;
    wire [33:0] score2={2'b0,pr2}+{2'b0,pg2}+{2'b0,pb2};
    reg strong3,possible3,saw_last;
    reg [3:0] pack_count;
    reg [31:0] pack_data;
    wire advance=!m_axis_tvalid || m_axis_tready;
    assign s_axis_tready=busy && !saw_last && advance;
    wire input_fire=s_axis_tvalid && s_axis_tready;
    wire expected_last=(rx_pixels+1'b1==n_pixels);
    wire [31:0] packed_next=pack_data | ({30'b0,possible3,strong3} << (pack_count*2));
    assign m_axis_tkeep=4'hf;
    always @(posedge clk) begin
        if (!rst_n) begin
            busy<=0; done<=0; errors<=0; rx_pixels<=0; tx_words<=0;
            frame_sequence<=0; strong_pixels<=0; possible_pixels<=0;
            v0<=0;v1<=0;v2<=0;v3<=0;last0<=0;last1<=0;last2<=0;last3<=0;
            saw_last<=0;pack_count<=0;pack_data<=0;
            m_axis_tvalid<=0;m_axis_tlast<=0;m_axis_tdata<=0;
            strong3<=0;possible3<=0;
        end else if (cmd_abort) begin
            busy<=0;done<=1;errors<=32'h20;
            v0<=0;v1<=0;v2<=0;v3<=0;saw_last<=0;
            pack_count<=0;pack_data<=0;m_axis_tvalid<=0;m_axis_tlast<=0;
        end else if (cmd_start && !busy) begin
            busy<=0;done<=0;errors<=0;rx_pixels<=0;tx_words<=0;
            strong_pixels<=0;possible_pixels<=0;
            v0<=0;v1<=0;v2<=0;v3<=0;saw_last<=0;
            pack_count<=0;pack_data<=0;m_axis_tvalid<=0;m_axis_tlast<=0;
            if (lut_loaded!=768 || noise_loaded!=48 || low_q16>high_q16) begin
                errors<=32'h04;done<=1;
            end else if (n_pixels==0 || n_pixels>384000) begin errors<=32'h10;done<=1; end
            else busy<=1;
        end else begin
            if (bad_cfg) errors<=errors|32'h04;
            if (cmd_start && busy) errors<=errors|32'h08;
            if (m_axis_tvalid && m_axis_tready) begin
                m_axis_tvalid<=0;tx_words<=tx_words+1'b1;
                if (m_axis_tlast) begin
                    busy<=0;done<=1;
                    if (errors==0 && !bad_cfg && !cmd_start) frame_sequence<=frame_sequence+1'b1;
                end
            end
            if (advance) begin
                v0<=input_fire;v1<=v0;v2<=v1;v3<=v2;
                if (input_fire) begin
                    r0<=lut_r[cur_r];g0<=lut_g[cur_g];b0<=lut_b[cur_b];
                    rr0<=ref_r;rg0<=ref_g;rb0<=ref_b;
                    ir0<=inv_r[ref_r[7:4]];ig0<=inv_g[ref_g[7:4]];ib0<=inv_b[ref_b[7:4]];
                    last0<=s_axis_tlast || expected_last;
                    rx_pixels<=rx_pixels+1'b1;
                    if (s_axis_tlast || expected_last) saw_last<=1;
                    if ((s_axis_tlast!=expected_last) || s_axis_tkeep!=4'hf)
                        errors<=errors | (s_axis_tlast!=expected_last ? 32'h01:0) |
                                           (s_axis_tkeep!=4'hf ? 32'h02:0) |
                                           (bad_cfg ? 32'h04:0) | (cmd_start ? 32'h08:0);
                end
                if (v0) begin sr1<=dr*dr;sg1<=dg*dg;sb1<=db*db;
                    ir1<=ir0;ig1<=ig0;ib1<=ib0;last1<=last0;end
                if (v1) begin pr2<=sr1*ir1;pg2<=sg1*ig1;pb2<=sb1*ib1;last2<=last1;end
                if (v2) begin strong3<=score2>{2'b0,high_q16};
                    possible3<=score2>{2'b0,low_q16};last3<=last2;end
                if (v3) begin
                    if (strong3) strong_pixels<=strong_pixels+1'b1;
                    if (possible3) possible_pixels<=possible_pixels+1'b1;
                    if (pack_count==15 || last3) begin
                        m_axis_tdata<=packed_next;m_axis_tvalid<=1;m_axis_tlast<=last3;
                        pack_data<=0;pack_count<=0;
                    end else begin pack_data<=packed_next;pack_count<=pack_count+1'b1;end
                end
            end
        end
    end
endmodule
