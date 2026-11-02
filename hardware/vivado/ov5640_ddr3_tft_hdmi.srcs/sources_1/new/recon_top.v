`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Module Name : recon_top
// Description : 三维重建核（PL 计算核）顶层
//
//      PS 侧  ──AXI4-Lite(0x4002_0000)──►  recon_axil
//                                              │ 掩膜写口
//                                              ▼
//                                          mask_ram (Block RAM)
//                                              │ 行读口
//                          ┌───────────────────┴──────────────┐
//                          │        voxel_carver              │  24 视角并行雕刻
//                          └───────────────────┬──────────────┘
//                                              │ 占据写口
//                                              ▼
//                                          occ_ram (Block RAM)
//                                              │
//      PS 侧  ◄──AXI4-Lite 读（OCC_RDATA）──────┘
//
//   一句话：PS 把「掩膜 + 系数」写进来，按一下 start，过一会儿把「体素占据位图」读回去。
//   整个测量计算都在 PL 里完成，不需要 PS 参与中间过程。
//////////////////////////////////////////////////////////////////////////////////
module recon_top #(
    parameter integer N  = 128,
    parameter integer MW = 128,
    parameter integer MH = 128,
    parameter integer NV = 24
)(
    input  wire        clk,
    input  wire        rst_n,

    // ---------------- AXI4-Lite 从机（PS 窗口）----------------
    input  wire [11:0] s_axi_awaddr,
    input  wire        s_axi_awvalid,
    output wire        s_axi_awready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wvalid,
    output wire        s_axi_wready,
    output wire [1:0]  s_axi_bresp,
    output wire        s_axi_bvalid,
    input  wire        s_axi_bready,
    input  wire [11:0] s_axi_araddr,
    input  wire        s_axi_arvalid,
    output wire        s_axi_arready,
    output wire [31:0] s_axi_rdata,
    output wire [1:0]  s_axi_rresp,
    output wire        s_axi_rvalid,
    input  wire        s_axi_rready
);

    localparam integer NWR   = MW/32;
    localparam integer VW    = $clog2(NV);
    localparam integer MAW   = $clog2(NV*MH*NWR+1);
    localparam integer OAW   = $clog2(N*N*N/32+1);
    localparam integer MASKW = NV*MH*NWR;
    localparam integer OCCW  = N*N*N/32;

    // ------------------------------------------------------------------
    // AXI 从机
    // ------------------------------------------------------------------
    wire               c_start, c_abort, c_busy, c_done;
    wire signed [31:0] c_kv, c_ofv;
    wire               c_cfg_we;
    wire [1:0]         c_cfg_sel;
    wire [VW-1:0]      c_cfg_view;
    wire signed [31:0] c_cfg_data;

    wire               mask_we;
    wire [MAW-1:0]     mask_waddr;
    wire [31:0]        mask_wdata;
    wire [31:0]        mask_wptr;

    wire [OAW-1:0]     occ_raddr;
    wire [31:0]        occ_rdata;
    wire [31:0]        occ_rptr;

    recon_axil #(.N(N), .MW(MW), .MH(MH), .NV(NV)) u_axil (
        .clk(clk), .rst_n(rst_n),
        .s_axi_awaddr(s_axi_awaddr), .s_axi_awvalid(s_axi_awvalid), .s_axi_awready(s_axi_awready),
        .s_axi_wdata(s_axi_wdata),   .s_axi_wstrb(s_axi_wstrb),     .s_axi_wvalid(s_axi_wvalid),
        .s_axi_wready(s_axi_wready),
        .s_axi_bresp(s_axi_bresp),   .s_axi_bvalid(s_axi_bvalid),   .s_axi_bready(s_axi_bready),
        .s_axi_araddr(s_axi_araddr), .s_axi_arvalid(s_axi_arvalid), .s_axi_arready(s_axi_arready),
        .s_axi_rdata(s_axi_rdata),   .s_axi_rresp(s_axi_rresp),     .s_axi_rvalid(s_axi_rvalid),
        .s_axi_rready(s_axi_rready),
        .c_start(c_start), .c_abort(c_abort), .c_busy(c_busy), .c_done(c_done),
        .c_kv(c_kv), .c_ofv(c_ofv),
        .c_cfg_we(c_cfg_we), .c_cfg_sel(c_cfg_sel),
        .c_cfg_view(c_cfg_view), .c_cfg_data(c_cfg_data),
        .mask_we(mask_we), .mask_waddr(mask_waddr), .mask_wdata(mask_wdata),
        .mask_wptr(mask_wptr),
        .occ_raddr(occ_raddr), .occ_rdata(occ_rdata), .occ_rptr(occ_rptr)
    );

    // ------------------------------------------------------------------
    // 掩膜 RAM：1 个写口（AXI）+ 1 个读口（雕刻引擎）
    // ------------------------------------------------------------------
    (* ram_style = "block" *) reg [31:0] mask_ram [0:MASKW-1];
    wire        [MAW-1:0] mrd_addr;
    wire        [31:0]    mrd_data;
    reg         [31:0]    mrd_q;

    always @(posedge clk) begin
        if (mask_we) mask_ram[mask_waddr] <= mask_wdata;
    end
    always @(posedge clk) mrd_q <= mask_ram[mrd_addr];
    assign mrd_data = mrd_q;

    // ------------------------------------------------------------------
    // 占据 RAM：1 个写口（雕刻引擎）+ 1 个读口（AXI 预取）
    // ------------------------------------------------------------------
    (* ram_style = "block" *) reg [31:0] occ_ram [0:OCCW-1];
    wire        occ_we;
    wire [OAW-1:0] occ_waddr;
    wire [31:0]    occ_wdata;
    reg  [31:0]    occ_rdata_r;

    always @(posedge clk) begin
        if (occ_we) occ_ram[occ_waddr] <= occ_wdata;
    end
    always @(posedge clk) occ_rdata_r <= occ_ram[occ_raddr];
    assign occ_rdata = occ_rdata_r;

    // ------------------------------------------------------------------
    // 雕刻引擎
    // ------------------------------------------------------------------
    voxel_carver #(.N(N), .MW(MW), .MH(MH), .NV(NV)) u_carver (
        .clk(clk), .rst_n(rst_n),
        .start(c_start), .abort(c_abort), .busy(c_busy), .done(c_done),
        .cfg_we(c_cfg_we), .cfg_sel(c_cfg_sel),
        .cfg_view(c_cfg_view), .cfg_data(c_cfg_data),
        .kv(c_kv), .ofv(c_ofv),
        .mrd_addr(mrd_addr), .mrd_data(mrd_data),
        .occ_we(occ_we), .occ_waddr(occ_waddr), .occ_wdata(occ_wdata)
    );

endmodule
