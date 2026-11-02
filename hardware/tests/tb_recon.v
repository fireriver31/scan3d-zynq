`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Testbench : tb_recon
// 作用      : 通过 AXI4-Lite 驱动整条链路，和 Python 参考模型【逐位对拍】
//
//   流程：复位 → 写系数 → 写掩膜 → 写 KV/OFV → 按 start → 轮询 done
//        → 读回全部占据字 → 与 mem/occ_expected.mem 比对 → 打印 PASS/FAIL
//
//   Run: python sim/run_core_rtl.py --vivado-bin <bin> --output <new-directory>
//////////////////////////////////////////////////////////////////////////////////
`include "mem/recon_cfg.vh"

module tb_recon;

    localparam integer N     = `RECON_N;
    localparam integer MW    = `RECON_MW;
    localparam integer MH    = `RECON_MH;
    localparam integer NV    = `RECON_NV;
    localparam integer MASKW = `RECON_MWORDS;
    localparam integer OCCW  = `RECON_OWORDS;

    reg clk = 1'b0;
    always #5 clk = ~clk;              // 100 MHz
    reg rst_n = 1'b0;

    // ---------------- AXI4-Lite ----------------
    reg  [11:0] awaddr;  reg awvalid; wire awready;
    reg  [31:0] wdata;   reg [3:0] wstrb; reg wvalid; wire wready;
    wire [1:0]  bresp;   wire bvalid; reg bready;
    reg  [11:0] araddr;  reg arvalid; wire arready;
    wire [31:0] rdata;   wire [1:0] rresp; wire rvalid; reg rready;

    recon_top #(.N(N), .MW(MW), .MH(MH), .NV(NV)) dut (
        .clk(clk), .rst_n(rst_n),
        .s_axi_awaddr(awaddr),  .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata),    .s_axi_wstrb(wstrb),     .s_axi_wvalid(wvalid),
        .s_axi_wready(wready),
        .s_axi_bresp(bresp),    .s_axi_bvalid(bvalid),   .s_axi_bready(bready),
        .s_axi_araddr(araddr),  .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata),    .s_axi_rresp(rresp),     .s_axi_rvalid(rvalid),
        .s_axi_rready(rready)
    );

    // ---------------- 测试向量 ----------------
    reg [31:0] mask_v [0:MASKW-1];
    reg [31:0] coef_v [0:2+3*NV-1];
    reg [31:0] exp_v  [0:OCCW-1];
    reg [31:0] got_v  [0:OCCW-1];
    reg [31:0] wrt_v  [0:OCCW-1];      // 直接抓雕刻引擎写出去的字

    integer i, k, errs, n_rtl, n_exp;
    integer werr, nw;
    reg [31:0] t;

    // 探针：直接盯雕刻引擎的写口
    initial begin
        nw = 0;
        for (i = 0; i < OCCW; i = i + 1) wrt_v[i] = 32'hXXXX_XXXX;
    end
    always @(posedge clk) if (dut.u_carver.occ_we) begin
        wrt_v[dut.u_carver.occ_waddr] = dut.u_carver.occ_wdata;
        nw = nw + 1;
    end

    // ---------------- AXI 任务 ----------------
    task axi_wr(input [11:0] a, input [31:0] d);
        begin
            while (!(awready && wready)) @(negedge clk);
            awaddr = a; awvalid = 1'b1;
            wdata  = d; wstrb = 4'hF; wvalid = 1'b1;
            @(negedge clk);
            awvalid = 1'b0; wvalid = 1'b0;
            while (!bvalid) @(negedge clk);
            bready = 1'b1;
            @(negedge clk);
            bready = 1'b0;
        end
    endtask

    task axi_rd(input [11:0] a, output [31:0] d);
        begin
            while (!arready) @(negedge clk);
            araddr = a; arvalid = 1'b1;
            @(negedge clk);
            arvalid = 1'b0;
            while (!rvalid) @(negedge clk);
            d = rdata;
            rready = 1'b1;
            @(negedge clk);
            rready = 1'b0;
        end
    endtask

    // ---------------- 主流程 ----------------
    initial begin
        awaddr = 0; awvalid = 0; wdata = 0; wstrb = 0; wvalid = 0; bready = 0;
        araddr = 0; arvalid = 0; rready = 0;

        $readmemh("mem/mask.mem",        mask_v);
        $readmemh("mem/coef_hex.mem",    coef_v);
        $readmemh("mem/occ_expected.mem", exp_v);

        $display("");
        $display("=====================================================");
        $display("  recon_top  --  RTL vs Python 逐位对拍");
        $display("  N=%0d  MW=%0d  MH=%0d  NV=%0d", N, MW, MH, NV);
        $display("-----------------------------------------------------");

        repeat (8) @(negedge clk);
        rst_n = 1'b1;
        repeat (4) @(negedge clk);

        // 1) 每视角系数
        axi_wr(12'h018, coef_v[0]);                    // KV
        axi_wr(12'h01C, coef_v[1]);                    // OFV
        for (i = 0; i < NV; i = i + 1) begin
            axi_wr(12'h100 + i*16 + 0, coef_v[2 + i*3 + 0]);   // KUX
            axi_wr(12'h100 + i*16 + 4, coef_v[2 + i*3 + 1]);   // KUY
            axi_wr(12'h100 + i*16 + 8, coef_v[2 + i*3 + 2]);   // OFU
        end

        // 2) 掩膜
        for (i = 0; i < MASKW; i = i + 1)
            axi_wr(12'h020, mask_v[i]);

        // 校验写指针
        errs = 0;
        axi_rd(12'h024, t);
        if (t !== MASKW) begin
            $display("  !! 掩膜写指针 = %0d，期望 %0d", t, MASKW);
            $fatal(1,"mask write pointer mismatch");
        end

        // 3) 开跑
        axi_wr(12'h000, 32'h0000_0001);                // start
        k = 0;
        t = 32'h0;
        while ((t[1] !== 1'b1) && (k < 20000000)) begin // 轮询 done
            repeat (500) @(negedge clk);
            k = k + 500;
            axi_rd(12'h004, t);
        end
        if (t[1] !== 1'b1) begin
            $display("  !! 超时：雕刻没有完成");
            $fatal(1,"carving timeout");
        end
        $display("  雕刻完成，仿真时间 = %0d ns", $time);

        // 4) 读回占据字
        axi_wr(12'h02C, 32'd0);
        repeat (4) @(negedge clk);
        for (i = 0; i < OCCW; i = i + 1) begin
            axi_rd(12'h030, got_v[i]);
        end

        // 5) 对拍
        // 5a) 写路径（雕刻引擎写出的字）vs 期望
        werr = 0;
        for (i = 0; i < OCCW; i = i + 1)
            if (wrt_v[i] !== exp_v[i]) begin
                if (werr < 8)
                    $display("    [写路径] 第 %0d 字: RTL=%08x 期望=%08x", i, wrt_v[i], exp_v[i]);
                werr = werr + 1;
            end
        $display("  写路径不匹配 : %0d / %0d   (occ_we 脉冲数 = %0d)", werr, OCCW, nw);

        // 5b) 读回路径 vs 期望
        errs = 0;
        n_rtl = 0;
        n_exp = 0;
        for (i = 0; i < OCCW; i = i + 1) begin
            if (got_v[i] !== exp_v[i]) begin
                if (errs < 8)
                    $display("    第 %0d 字不匹配: RTL=%08x  期望=%08x", i, got_v[i], exp_v[i]);
                errs = errs + 1;
            end
            for (k = 0; k < 32; k = k + 1) begin
                n_rtl = n_rtl + got_v[i][k];
                n_exp = n_exp + exp_v[i][k];
            end
        end

        $display("-----------------------------------------------------");
        $display("  RTL  占据体素 : %0d", n_rtl);
        $display("  Python 占据体素: %0d", n_exp);
        $display("  不匹配字数    : %0d / %0d", errs, OCCW);
        if (errs == 0 && werr == 0 && nw == OCCW) $display("  >>> PASS   逐位与 Python 参考模型完全一致");
        else           $display("  >>> FAIL   有 %0d 个字对不上", errs);
        $display("=====================================================");
        $display("");
        if (errs != 0 || werr != 0 || nw != OCCW) $fatal(1,"occupancy read/write mismatch");
        $finish;
    end

    // 超时保护
    initial begin
        #2000000000;
        $display("  !! 仿真超时");
        $fatal(1,"simulation timeout");
    end

endmodule
