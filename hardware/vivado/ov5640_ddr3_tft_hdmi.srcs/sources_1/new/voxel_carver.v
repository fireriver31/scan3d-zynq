`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Module Name : voxel_carver
// Description : 体素雕刻引擎（visual hull / 多视角剪影求交）
//
//   【数学】正交投影下，竖直坐标 v 只跟 z 有关：
//        v = (OFV - z*KV) >> 16
//   而水平坐标 u 是 x,y 的仿射函数：
//        u = (OFU + x*KUX + y*KUY) >> 16
//   一个体素被保留 <=> 在所有视角里都落在"物体"上（掩膜位 == 0）。
//
//   【为什么快】把 NV 个视角【全部并行】：
//     - 每个 (z,y,x) 只算一次，占据位只写一遍，不用读-改-写；
//     - 一个 z 层只取一行掩膜（v 相同），24 个视角的行并排放在寄存器里；
//     - u 用累加器推进，不用乘法器：每拍就是 24 个加法器 + 24 个取位。
//   总共约 N*N*N 拍（另加每层取行的开销）。
//
//   【系数是 Q16 定点，由 PS(Python) 算好下发】—— 换标定/换转轴只改系数，
//   RTL 一行不动。RTL 只做「加 + 算术右移 + 比大小 + 取一位」。
//
//   掩膜约定：1 = 亮 = 背景； 0 = 暗 = 物体。
//   越界（u 或 v 超出掩膜）一律当背景 -> 体素清掉（保守，绝不虚增体积）。
//////////////////////////////////////////////////////////////////////////////////
module voxel_carver #(
    parameter integer N  = 128,                  // 体素网格边长（须为 32 的倍数）
    parameter integer MW = 128,                  // 掩膜宽（须为 32 的倍数）
    parameter integer MH = 128,                  // 掩膜高
    parameter integer NV = 24,                   // 视角数
    parameter integer NWR = MW/32,               // 每行掩膜字数
    parameter integer IDXW = $clog2(MW),         // 掩膜列索引位宽
    parameter integer VWID = $clog2(MH),         // 掩膜行索引位宽
    parameter integer VW   = $clog2(NV),         // 视角索引位宽
    parameter integer LDW  = $clog2(NV*NWR+1)+1,
    parameter integer MAW  = $clog2(NV*MH*NWR+1),
    parameter integer OAW  = $clog2(N*N*N/32+1),
    parameter integer CW   = $clog2(N)+1
)(
    input  wire               clk,
    input  wire               rst_n,

    input  wire               start,       // 单拍脉冲
    input  wire               abort,
    output reg                busy,
    output reg                done,

    // ---- 每视角系数写口（来自 AXI 寄存器区）----
    input  wire               cfg_we,
    input  wire [1:0]         cfg_sel,     // 0=KUX 1=KUY 2=OFU
    input  wire [VW-1:0]      cfg_view,
    input  wire signed [31:0] cfg_data,

    // ---- 全局几何（只跟 z 有关）----
    input  wire signed [31:0] kv,
    input  wire signed [31:0] ofv,

    // ---- 掩膜读口（一行 NWR 个字）----
    output reg  [MAW-1:0]     mrd_addr,
    input  wire [31:0]        mrd_data,

    // ---- 占据写口 ----
    output reg                occ_we,
    output reg  [OAW-1:0]     occ_waddr,
    output reg  [31:0]        occ_wdata
);

    localparam integer OWPC = N/32;                 // 每行占据字数
    localparam integer LDTOT = NV*NWR;              // 一次要取多少个掩膜字

    // ------------------------------------------------------------------
    // 状态机
    // ------------------------------------------------------------------
    localparam [2:0] S_IDLE  = 3'd0,
                     S_ZROW  = 3'd1,
                     S_LOAD  = 3'd2,
                     S_YPREP = 3'd3,
                     S_X     = 3'd4,
                     S_ZEND  = 3'd5;

    reg [2:0]          st;
    reg signed [31:0]  zc;                 // 有符号，便于算 v
    reg [CW-1:0]       yc, xc;
    reg [4:0]          wbit;
    reg [31:0]         oword;
    reg                v_ok;
    reg [VWID-1:0]     v_val;

    reg [LDW-1:0]      ld_cnt;
    reg                ld_sh;              // 本拍 mrd_data 有效
    reg [VW-1:0]       ld_vi;              // 本拍移入哪一视角

    // ------------------------------------------------------------------
    // 每视角几何系数 + 行缓冲（寄存器阵列：必须能"同时读全部"）
    //   行缓冲用右移方式装入：读进来的字从高位挤入，装完后
    //   rbuf[vi][u] 就是掩膜第 u 列。省掉变量字选，综合更省。
    // ------------------------------------------------------------------
    reg signed [31:0]  kux_a [0:NV-1];
    reg signed [31:0]  kuy_a [0:NV-1];
    reg signed [31:0]  ofu_a [0:NV-1];
    reg [MW-1:0]       rbuf  [0:NV-1];
    reg signed [31:0]  ucur  [0:NV-1];
    reg signed [31:0]  ubase [0:NV-1];

    integer ci, li;
    reg signed [31:0]  us;
    reg                okb, btb, keepl;

    // 系数写
    always @(posedge clk) begin
        if (cfg_we) begin
            for (ci = 0; ci < NV; ci = ci + 1) begin
                if (ci[VW-1:0] == cfg_view) begin
                    if      (cfg_sel == 2'd0) kux_a[ci] <= cfg_data;
                    else if (cfg_sel == 2'd1) kuy_a[ci] <= cfg_data;
                    else if (cfg_sel == 2'd2) ofu_a[ci] <= cfg_data;
                end
            end
        end
    end

    // v 的运算
    wire signed [31:0] vraw = ofv - (zc * kv);
    wire signed [31:0] vsh  = vraw >>> 16;
    wire               vokc = (vsh >= 0) && (vsh < MH);

    // ------------------------------------------------------------------
    // 主状态机
    // ------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            st <= S_IDLE; busy <= 1'b0; done <= 1'b0;
            zc <= 32'sd0; yc <= {CW{1'b0}}; xc <= {CW{1'b0}};
            wbit <= 5'd0; oword <= 32'd0;
            v_ok <= 1'b0; v_val <= {VWID{1'b0}};
            ld_cnt <= {LDW{1'b0}}; ld_sh <= 1'b0; ld_vi <= {VW{1'b0}};
            mrd_addr <= {MAW{1'b0}};
            occ_we <= 1'b0; occ_waddr <= {OAW{1'b0}}; occ_wdata <= 32'd0;
            for (li = 0; li < NV; li = li + 1) begin
                rbuf[li]  <= {MW{1'b1}};        // 默认全背景
                ucur[li]  <= 32'sd0;
                ubase[li] <= 32'sd0;
            end
        end else if (abort && st != S_IDLE) begin
            st <= S_IDLE; busy <= 1'b0; occ_we <= 1'b0;
        end else begin
            occ_we <= 1'b0;

            case (st)
            // ----------------------------------------------------------
            S_IDLE: begin
                if (start) begin
                    zc <= 32'sd0; yc <= {CW{1'b0}}; xc <= {CW{1'b0}};
                    busy <= 1'b1; done <= 1'b0;
                    st   <= S_ZROW;
                end
            end
            // ----------------------------------------------------------
            // 每个 z 层：算 v，并把 ubase 复位成 u(x=0,y=0)
            S_ZROW: begin
                v_ok  <= vokc;
                v_val <= vsh[VWID-1:0];
                for (li = 0; li < NV; li = li + 1) ubase[li] <= ofu_a[li];
                yc    <= {CW{1'b0}};
                ld_cnt <= {LDW{1'b0}};
                ld_sh  <= 1'b0;
                ld_vi  <= {VW{1'b0}};
                if (vokc) st <= S_LOAD;
                else      st <= S_YPREP;      // 整层越界 -> 全清（keepl=0）
            end
            // ----------------------------------------------------------
            // 取 NV 个视角在这个 v 上的一整行掩膜
            //   t=0..95 逐拍发地址；BRAM 读出比地址晚 2 拍，
            //   所以数据从 t=2 起才有效，最后一字在 t=97 收到。
            //   行缓冲用【右移】装入：先来的字往低位沉，
            //   装完后 rbuf[vi][u] 就是掩膜第 u 列，索引就是 u 本身。
            S_LOAD: begin
                if (ld_cnt < LDTOT[LDW-1:0])
                    mrd_addr <= ((ld_cnt / NWR) * (MH*NWR)) + (v_val * NWR) + (ld_cnt % NWR);
                for (li = 0; li < NV; li = li + 1)
                    if (ld_sh && (li[VW-1:0] == ld_vi))
                        rbuf[li] <= {mrd_data, rbuf[li][MW-1:32]};
                ld_sh  <= (ld_cnt != {LDW{1'b0}});
                ld_vi  <= (ld_cnt == {LDW{1'b0}}) ? {VW{1'b0}}
                                                  : ((ld_cnt - 1'b1) / NWR);
                ld_cnt <= ld_cnt + 1'b1;
                if (ld_cnt == LDTOT[LDW-1:0] + 1'b1) st <= S_YPREP;
            end
            // ----------------------------------------------------------
            // 每个 y 行：把 u 复位到本行起点，然后 y 方向推进 KUY
            S_YPREP: begin
                for (li = 0; li < NV; li = li + 1) begin
                    ucur[li]  <= ubase[li];
                    ubase[li] <= ubase[li] + kuy_a[li];
                end
                xc <= {CW{1'b0}}; wbit <= 5'd0; oword <= 32'd0;
                st <= S_X;
            end
            // ----------------------------------------------------------
            // 一行 x：每拍一个体素（NV 个视角同时并行判断）
            S_X: begin
                keepl = v_ok;
                for (li = 0; li < NV; li = li + 1) begin
                    us  = ucur[li] >>> 16;
                    okb = (us >= 0) && (us < MW);
                    btb = rbuf[li][us[IDXW-1:0]];
                    keepl = keepl && okb && (btb == 1'b0);
                    ucur[li] <= ucur[li] + kux_a[li];
                end

                if (wbit == 5'd31) begin
                    occ_we    <= 1'b1;
                    occ_waddr <= (((zc * N) + yc) * OWPC) + (xc >> 5);
                    occ_wdata <= oword | (keepl ? (32'd1 << wbit) : 32'd0);
                    oword     <= 32'd0;
                    wbit      <= 5'd0;
                end else begin
                    oword <= oword | (keepl ? (32'd1 << wbit) : 32'd0);
                    wbit  <= wbit + 5'd1;
                end

                if (xc == N[CW-1:0] - 1'b1) begin
                    xc <= {CW{1'b0}};
                    if (yc == N[CW-1:0] - 1'b1) begin
                        yc <= {CW{1'b0}};
                        st <= S_ZEND;
                    end else begin
                        yc <= yc + 1'b1;
                        st <= S_YPREP;
                    end
                end else begin
                    xc <= xc + 1'b1;
                end
            end
            // ----------------------------------------------------------
            S_ZEND: begin
                if (zc == N[31:0] - 1) begin
                    busy <= 1'b0; done <= 1'b1; st <= S_IDLE;
                end else begin
                    zc <= zc + 32'sd1;
                    st <= S_ZROW;
                end
            end
            default: st <= S_IDLE;
            endcase
        end
    end

endmodule
