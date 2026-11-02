`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Module Name : recon_axil
// Description : 三维重建核的 AXI4-Lite 从机（PS 侧寄存器窗口）
//
//   基地址：0x4002_0000（在 BD 的 Address Editor 里分配）
//
//   偏移    名称          读写  说明
//   ------------------------------------------------------------------------
//   0x000   CTRL          W    bit0 = start（自清）；bit1 = abort（自清）
//   0x004   STATUS        R    bit0 = busy；bit1 = done
//   0x008   CFG_N         R    体素网格边长
//   0x00C   CFG_MW        R    掩膜宽
//   0x010   CFG_MH        R    掩膜高
//   0x014   CFG_NV        R    视角数
//   0x018   COEF_KV       R/W  Q16， v = (OFV - z*KV) >> 16
//   0x01C   COEF_OFV      R/W  Q16
//   0x020   MASK_WDATA    W    写一个掩膜字，写指针自动 +1
//   0x024   MASK_WPTR     R/W  掩膜写指针（字序号）
//   0x028   MASK_WCNT     R    掩膜总字数
//   0x02C   OCC_RPTR      R/W  占据读指针（字序号）
//   0x030   OCC_RDATA     R    返回当前指针的字，并把指针 +1
//   0x034   OCC_WCNT     R    占据总字数
//   0x100+v*16+{0,4,8}     R/W  视角 v 的 KUX / KUY / OFU（Q16，有符号）
//
//   ★ 系数全部由 PS(Python) 算好下发；RTL 不关心标定怎么来的。
//
//   ★ 两个容易踩的坑（本文件已经处理好）：
//     ① 写地址/写数据必须在【握手那一拍】锁存下来再用。AXI 规定握手完成后
//        主机可以把 AWADDR/WDATA 立刻换掉，直接拿输入信号去译码会认错寄存器。
//     ② occ_rptr 只能有一个 always 块驱动（写指针 + 读自增），
//        分成两个块在仿真里能过，综合会报 multi-driven。
//////////////////////////////////////////////////////////////////////////////////
module recon_axil #(
    parameter integer N  = 128,
    parameter integer MW = 128,
    parameter integer MH = 128,
    parameter integer NV = 24,
    parameter integer VW   = $clog2(NV),
    parameter integer MAW  = $clog2(NV*MH*(MW/32)+1),
    parameter integer OAW  = $clog2(N*N*N/32+1)
)(
    input  wire        clk,
    input  wire        rst_n,

    // ---------------- AXI4-Lite 从机 ----------------
    input  wire [11:0] s_axi_awaddr,
    input  wire        s_axi_awvalid,
    output reg         s_axi_awready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wvalid,
    output reg         s_axi_wready,
    output reg  [1:0]  s_axi_bresp,
    output reg         s_axi_bvalid,
    input  wire        s_axi_bready,
    input  wire [11:0] s_axi_araddr,
    input  wire        s_axi_arvalid,
    output reg         s_axi_arready,
    output reg  [31:0] s_axi_rdata,
    output reg  [1:0]  s_axi_rresp,
    output reg         s_axi_rvalid,
    input  wire        s_axi_rready,

    // ---------------- 到雕刻引擎 ----------------
    output wire               c_start,
    output wire               c_abort,
    input  wire               c_busy,
    input  wire               c_done,
    output reg  signed [31:0] c_kv,
    output reg  signed [31:0] c_ofv,
    output wire               c_cfg_we,
    output wire [1:0]         c_cfg_sel,
    output wire [VW-1:0]      c_cfg_view,
    output wire signed [31:0] c_cfg_data,

    // ---------------- 掩膜 RAM 写口 ----------------
    output reg                mask_we,
    output reg  [MAW-1:0]     mask_waddr,
    output reg  [31:0]        mask_wdata,
    output reg  [31:0]        mask_wptr,

    // ---------------- 占据 RAM 读口 ----------------
    output wire [OAW-1:0]     occ_raddr,
    input  wire [31:0]        occ_rdata,
    output wire [31:0]        occ_rptr
);

    localparam integer MASKW = NV*MH*(MW/32);
    localparam integer OCCW  = N*N*N/32;

    reg        start_r, abort_r;
    reg [31:0] occ_rptr_r;

    // 写事务握手 / 执行
    reg        aw_done, w_done;
    reg [11:0] awaddr_l;
    reg [31:0] wdata_l;
    wire       wr_exec = aw_done && w_done && !s_axi_bvalid;

    // 读事务握手
    wire       rd_fire = s_axi_arready && s_axi_arvalid;

    wire       wr_is_coef = (awaddr_l >= 12'h100) && (awaddr_l < 12'h300);
    wire       rd_is_coef = (s_axi_araddr >= 12'h100) && (s_axi_araddr < 12'h300);

    // 系数写口（用锁存后的地址/数据译码，同一拍被雕刻引擎取走）
    assign c_cfg_we   = wr_exec && wr_is_coef;
    assign c_cfg_sel  = (awaddr_l >> 2) & 2'b11;
    assign c_cfg_view = (awaddr_l - 12'h100) >> 4;
    assign c_cfg_data = wdata_l;

    assign c_start = start_r;
    assign c_abort = abort_r;

    // occ_raddr 组合指向读指针；RAM 输出已带一拍寄存，
    // 所以 occ_rdata 就是 occ[occ_rptr]，不能再加寄存器（否则慢一个字）。
    assign occ_raddr = occ_rptr_r[OAW-1:0];
    assign occ_rptr  = occ_rptr_r;

    // ------------------------------------------------------------------
    // occ 读指针：唯一驱动源（本块）
    //   优先级：主机显式写指针 > 读一次自增
    // ------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) occ_rptr_r <= 32'd0;
        else if (wr_exec && (awaddr_l == 12'h02C)) occ_rptr_r <= wdata_l;
        else if (rd_fire && (s_axi_araddr == 12'h030)) occ_rptr_r <= occ_rptr_r + 32'd1;
    end

    // ------------------------------------------------------------------
    // 读数据组合译码（用"当拍"的 AXI 地址 —— 握手那一拍地址一定有效）
    // ------------------------------------------------------------------
    reg [31:0] rdv;
    always @(*) begin
        rdv = 32'hDEAD_0000 | {20'd0, s_axi_araddr};
        if (rd_is_coef) rdv = 32'h0;                 // 系数不回读
        else case (s_axi_araddr)
            12'h004: rdv = {30'd0, c_done, c_busy};
            12'h008: rdv = N;
            12'h00C: rdv = MW;
            12'h010: rdv = MH;
            12'h014: rdv = NV;
            12'h018: rdv = c_kv;
            12'h01C: rdv = c_ofv;
            12'h024: rdv = mask_wptr;
            12'h028: rdv = MASKW;
            12'h02C: rdv = occ_rptr_r;
            12'h030: rdv = occ_rdata;                // RAM 输出已寄存一拍
            12'h034: rdv = OCCW;
            default: ;
        endcase
    end

    // ------------------------------------------------------------------
    // 写通道：握手 -> 锁存 -> 执行 -> 回 B
    // ------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s_axi_awready <= 1'b1; s_axi_wready <= 1'b1;
            s_axi_bvalid  <= 1'b0; s_axi_bresp  <= 2'b00;
            aw_done <= 1'b0; w_done <= 1'b0;
            awaddr_l <= 12'd0; wdata_l <= 32'd0;
            mask_we <= 1'b0; mask_waddr <= {MAW{1'b0}}; mask_wdata <= 32'd0;
            mask_wptr <= 32'd0;
            c_kv <= 32'sd0; c_ofv <= 32'sd0;
            start_r <= 1'b0; abort_r <= 1'b0;
        end else begin
            mask_we <= 1'b0;
            start_r <= 1'b0;
            abort_r <= 1'b0;

            if (s_axi_awready && s_axi_awvalid) begin
                s_axi_awready <= 1'b0; aw_done <= 1'b1; awaddr_l <= s_axi_awaddr;
            end
            if (s_axi_wready && s_axi_wvalid) begin
                s_axi_wready <= 1'b0;  w_done  <= 1'b1; wdata_l  <= s_axi_wdata;
            end

            if (wr_exec) begin
                s_axi_bvalid <= 1'b1;
                s_axi_bresp  <= 2'b00;
                if (wr_is_coef) begin
                    // 由 c_cfg_* 组合送给雕刻引擎，这里不做
                end else case (awaddr_l)
                    12'h000: begin
                        if (wdata_l[0]) start_r <= 1'b1;
                        if (wdata_l[1]) abort_r <= 1'b1;
                    end
                    12'h018: c_kv  <= wdata_l;
                    12'h01C: c_ofv <= wdata_l;
                    12'h020: begin
                        mask_we    <= 1'b1;
                        mask_waddr <= mask_wptr[MAW-1:0];
                        mask_wdata <= wdata_l;
                        mask_wptr  <= mask_wptr + 32'd1;
                    end
                    12'h024: mask_wptr <= wdata_l;
                    default: ;
                endcase
            end

            if (s_axi_bvalid && s_axi_bready) begin
                s_axi_bvalid  <= 1'b0;
                s_axi_awready <= 1'b1;
                s_axi_wready  <= 1'b1;
                aw_done <= 1'b0;
                w_done  <= 1'b0;
            end
        end
    end

    // ------------------------------------------------------------------
    // 读通道
    // ------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s_axi_arready <= 1'b1; s_axi_rvalid <= 1'b0;
            s_axi_rdata <= 32'd0;  s_axi_rresp  <= 2'b00;
        end else begin
            if (rd_fire) begin
                s_axi_arready <= 1'b0;
                s_axi_rdata   <= rdv;
                s_axi_rvalid  <= 1'b1;
                s_axi_rresp   <= 2'b00;
            end
            if (s_axi_rvalid && s_axi_rready) begin
                s_axi_rvalid  <= 1'b0;
                s_axi_arready <= 1'b1;
            end
        end
    end

endmodule
