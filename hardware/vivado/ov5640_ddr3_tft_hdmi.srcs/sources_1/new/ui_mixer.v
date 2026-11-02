`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company       : Scan3D project (ACZ702 camera/display integration)
// Module Name   : ui_mixer
// Description   : UI 图层（OSD）叠加混合器，把 PS 写进 BRAM 的 UI 位图叠加到摄像头画面上
//
//   输入：
//     disp_red/green/blue + disp_de  —— disp_driver 输出的摄像头画面（RGB565，DE 有效区 800x480）
//     h_addr / v_addr                —— disp_driver 输出的像素坐标（12bit）
//     bram_dout                      —— 片内真双口 BRAM 的 PortB 读数据（32bit，AXI 侧写、这里只读）
//   输出：
//     out_red/green/blue             —— 叠加后的 RGB565，与 TFT_de 严格同拍（组合输出，无像素延迟）
//     bram_en / bram_addr            —— PortB 读使能 / 字地址
//
// ------------------------------------------------------------------------------
// 【UI 位图格式】（Python 端 ui_overlay.py 必须与此完全一致）
//   每像素 2 bit，1 个 32bit 字放 16 个像素：
//     字地址  = y * 50 + (x / 16)        (y=0..479, x=0..799, 共 50 字/行, 24000 字)
//     位位置  = bit[2*(x%16) + 1 : 2*(x%16)]，字内最左边的像素占最低 2 bit（小端）
//     取值    ：2'b00 = 透明（显示摄像头画面）
//               2'b01 = 前景色 COL_FG （默认白，画文字）
//               2'b10 = 背景色 COL_BG （默认黑，画文字描边 / 面板）
//               2'b11 = 强调色 COL_ACC（默认黄，画标题 / 高亮）
//   PS 侧字节地址 = 0x4000_0000 + 4 * 字地址（AXI BRAM Controller，128KB = 32768 字）
//   字地址 24000..32767 不参与显示，可当自检/回读用的草稿区。
// ------------------------------------------------------------------------------
// 【为什么这样对齐】disp_driver 里：
//     H_Addr = 实际列号 + 1（DE 打了一拍，H_Addr 是组合的）
//     V_Addr = 实际行号
//   而 BRAM 读延迟 1 拍。于是在第 T 拍用“当前 h_addr”发起读，T+1 拍拿到的数据
//   正好对应 T+1 拍要显示的那个像素 —— 读延迟与 +1 偏移互相抵消，因此输出可以
//   完全组合地混出来，摄像头画面不会被整体平移 1 个像素。
//////////////////////////////////////////////////////////////////////////////////

module ui_mixer #(
  parameter KEY_ENABLE=1,
  parameter SCREEN_W  = 800,          // UI 图层宽（像素）
  parameter SCREEN_H  = 480,          // UI 图层高（像素）
  parameter KEY_COLOR = 16'hF81F,     // 摄像头层抠像色（命中则输出黑色；默认品红，几乎不会命中）
  parameter COL_FG    = 16'hFFFF,     // 2'b01 白色
  parameter COL_BG    = 16'h0000,     // 2'b10 黑色
  parameter COL_ACC   = 16'hFFE0      // 2'b11 黄色
)(
  input             clk,              // 像素时钟 disp_clk（800x480 约 33MHz）
  input             reset,            // 高有效同步复位

  input      [4:0]  disp_red,         // 摄像头层 / 背景层
  input      [5:0]  disp_green,
  input      [4:0]  disp_blue,
  input             disp_de,
  input      [11:0] h_addr,
  input      [11:0] v_addr,

  output     [4:0]  out_red,          // 混合结果，直接接 TFT/HDMI
  output     [5:0]  out_green,
  output     [4:0]  out_blue,

  output            bram_en,          // BRAM PortB 读使能（一直读，PortB 只用作读）
  output     [16:0] bram_addr,        // BRAM PortB 地址：★字节地址★，低 2 位恒 0（见文件末尾说明）
  input      [31:0] bram_dout         // BRAM PortB 读数据
);

  // ------------------------------------------------------------------
  // 参数
  // ------------------------------------------------------------------
  localparam BPP  = 2;                                  // 每像素 2bit
  localparam PPW  = 32 / BPP;                           // 每个 32bit 字放 16 像素
  localparam WPR  = (SCREEN_W + PPW - 1) / PPW;         // 每行字数 = 50
  localparam WTOT = WPR * SCREEN_H;                     // 显示区总字数 = 24000

  // ------------------------------------------------------------------
  // 行基址跟踪
  //   line_base 当前正在显示的行在 BRAM 中的起始字地址
  //   nxt_base  下一行的起始字地址（空档期发起的那次读用的就是它）
  //   sel       本次读的像素落在字内的第几个（下一拍取数据时用）
  // ------------------------------------------------------------------
  reg        de_q;                     // disp_de 打一拍，用于检测“新的一行第一个有效像素”
  reg [14:0] line_base;
  reg [14:0] nxt_base;
  reg [3:0]  sel;

  wire newline = disp_de & ~de_q;                                   // 行首
  wire [14:0] base_new = (v_addr == 12'd0) ? 15'd0 : nxt_base;       // 新行的基址（每帧从 v_addr==0 自动重同步）
  wire [14:0] rd_base  = newline ? base_new : (disp_de ? line_base : nxt_base);

  // 下一行的基址，到帧尾回卷到 0：
  //   最后一行的消隐期内 rd_base = nxt_base，而那时“下一行”已经是新一帧的第 0 行，
  //   所以必须回卷成 0，否则每帧 (0,0) 这个像素会取到越界数据。
  wire [14:0] nxt_inc  = nxt_base + WPR;
  wire [14:0] nxt_wrap = (nxt_inc >= WTOT) ? 15'd0 : nxt_inc;

  always @(posedge clk) begin
    if (reset) begin
      de_q      <= 1'b0;
      line_base <= 15'd0;
      nxt_base  <= 15'd0;             // 复位后先显示第 0 行，消隐期读的就是第 0 行
      sel       <= 4'd0;
    end
    else begin
      de_q <= disp_de;
      sel  <= h_addr[3:0];            // 本拍读的像素在字内的位置，下一拍用

      if (newline) begin
        if (v_addr == 12'd0) begin    // 新的一帧第 0 行：基址归零，自同步
          line_base <= 15'd0;
          nxt_base  <= WPR;
        end
        else begin
          line_base <= nxt_base;
          nxt_base  <= nxt_wrap;
        end
      end
    end
  end

  // ------------------------------------------------------------------
  // 地址：BD 里的 blk_mem_gen_0 勾选了 “Use 32-bit Address”
  //       (C_ENABLE_32BIT_ADDRESS=1, C_ADDRA_WIDTH/C_ADDRB_WIDTH=32)，
  //       它的 addra/addrb 是 32 位【字节地址】，IP 内部会把地址 >>2 变成字地址。
  //       （AXI 那侧的 axi_bram_ctrl 输出的就是 17 位字节地址，所以 PS 写入没问题。）
  //       因此这里必须输出 字地址<<2，否则字地址会被再除以 4 ——
  //       现象就是整个 UI 图层被放大 4 倍（左上角 16x16 方块变 64x64、
  //       中间的行/列跑到屏幕外、同一行的字在屏幕上重复 4 次）。
  // ------------------------------------------------------------------
  wire [14:0] word_addr = rd_base + h_addr[11:4];

  assign bram_en   = 1'b1;
  assign bram_addr = {word_addr, 2'b00};          // 字地址 -> 字节地址

  // ------------------------------------------------------------------
  // 取 UI 像素并混合
  //   bram_dout 是本拍有效的字（对应本拍显示的像素），sel 是上一拍锁存的字内位置
  // ------------------------------------------------------------------
  wire [31:0] ui_shift = bram_dout >> {sel, 1'b0};
  wire [1:0]  ui_pix   = ui_shift[1:0];

  wire [15:0] cam_pix = {disp_red, disp_green, disp_blue};
  wire [15:0] mix_pix = (ui_pix == 2'b00) ? ((KEY_ENABLE && cam_pix == KEY_COLOR) ? COL_BG : cam_pix) :
                        (ui_pix == 2'b01) ? COL_FG :
                        (ui_pix == 2'b10) ? COL_BG : COL_ACC;

  assign {out_red, out_green, out_blue} = disp_de ? mix_pix : 16'h0000;

endmodule
