`timescale 1ns / 1ps

// Camera RGB565 stream -> binary foreground mask in RGB565 form.
// First complete valid frame after reset is the EMPTY background reference.
// DVP_Capture supplies one-based x/y coordinates with each valid pixel.
module rgb565_mask_stream #(
    parameter integer WIDTH = 800,
    parameter integer HEIGHT = 480,
    parameter integer BG_X_STEP = 5,
    parameter integer BG_Y_STEP = 4,
    parameter integer COLOR_THRESHOLD = 18
)(
    input  wire        pclk,
    input  wire        rst,
    input  wire        frame_vs,
    input  wire        pixel_valid,
    input  wire [15:0] pixel_rgb565,
    input  wire [11:0] pixel_x_one_based,
    input  wire [11:0] pixel_y_one_based,
    output reg         mask_valid,
    output wire [15:0] mask_rgb565,
    output reg         background_ready
);
    localparam integer BG_WIDTH  = (WIDTH + BG_X_STEP - 1) / BG_X_STEP;
    localparam integer BG_HEIGHT = (HEIGHT + BG_Y_STEP - 1) / BG_Y_STEP;
    localparam integer BG_SIZE   = BG_WIDTH * BG_HEIGHT;
    localparam integer BG_AW     = $clog2(BG_SIZE);

    // Reset originates in the PS/PLL clock domain. Assert immediately but
    // release it only after two camera-PCLK edges to avoid a CDC release race.
    (* ASYNC_REG = "TRUE" *) reg [1:0] reset_pipe;
    always @(posedge pclk or posedge rst) begin
        if (rst) reset_pipe <= 2'b11;
        else     reset_pipe <= {reset_pipe[0], 1'b0};
    end
    wire rst_pclk = reset_pipe[1];

    // Coarse empty-scene reference: one RGB565 sample per 5x4 input pixels
    // with the default parameters. 19,200 x 16 bits rather than a full frame.
    (* ram_style = "block" *) reg [15:0] background [0:BG_SIZE-1];
    reg [15:0] bg_read;
    reg frame_vs_q;
    reg background_frame;
    reg frame_seen;
    reg [3:0] x_phase;
    reg [11:0] grid_x;

    wire [11:0] x_zero = pixel_x_one_based - 12'd1;
    wire [11:0] y_zero = pixel_y_one_based - 12'd1;
    wire [11:0] this_grid_x = (pixel_x_one_based == 12'd1) ? 12'd0 : grid_x;
    wire [11:0] this_grid_y = y_zero / BG_Y_STEP;
    wire [BG_AW-1:0] bg_address = this_grid_y * BG_WIDTH + this_grid_x;
    wire usable_pixel = pixel_valid &&
                        pixel_x_one_based >= 1 && pixel_x_one_based <= WIDTH &&
                        pixel_y_one_based >= 1 && pixel_y_one_based <= HEIGHT;
    wire bg_sample = usable_pixel &&
                     ((pixel_x_one_based == 12'd1) || (x_phase == 0)) &&
                     ((y_zero % BG_Y_STEP) == 0);

    // S0: BRAM read and input alignment. The first usable frame is stored as
    // background. Later frames read that stored background without changing it.
    reg        v0;
    reg        forced_white0;
    reg [15:0] px0;
    reg [11:0] x0, y0;
    always @(posedge pclk) begin
        if (rst_pclk) begin
            frame_vs_q       <= 1'b0;
            frame_seen       <= 1'b0;
            background_frame <= 1'b0;
            background_ready <= 1'b0;
            x_phase          <= 0;
            grid_x           <= 0;
            bg_read          <= 0;
            v0               <= 0;
            forced_white0    <= 1'b1;
            px0              <= 0;
            x0               <= 0;
            y0               <= 0;
        end else begin
            frame_vs_q <= frame_vs;
            if (frame_vs && !frame_vs_q) begin
                if (!frame_seen) begin
                    frame_seen       <= 1'b1;
                    background_frame <= 1'b1;
                end else if (background_frame) begin
                    background_frame <= 1'b0;
                    background_ready <= 1'b1;
                end
            end
            v0 <= usable_pixel;
            if (usable_pixel) begin
                px0           <= pixel_rgb565;
                x0            <= x_zero;
                y0            <= y_zero;
                forced_white0 <= !background_ready;
                bg_read       <= background[bg_address];
                if (background_frame && bg_sample)
                    background[bg_address] <= pixel_rgb565;

                // The DVP stream has one valid beat per RGB565 pixel, even
                // though the camera uses two PCLK cycles to send that pixel.
                if (pixel_x_one_based == 12'd1) begin
                    x_phase <= 1;
                    grid_x  <= 0;
                end else if (x_phase == BG_X_STEP-1) begin
                    x_phase <= 0;
                    grid_x  <= grid_x + 1'b1;
                end else begin
                    x_phase <= x_phase + 1'b1;
                end
            end
        end
    end

    // S1: RGB565 channel distance. Threshold is in native 5/6/5 units;
    // larger numbers reject more illumination noise but lose subtle objects.
    wire [5:0] dr = (px0[15:11] >= bg_read[15:11]) ?
                    (px0[15:11] - bg_read[15:11]) :
                    (bg_read[15:11] - px0[15:11]);
    wire [5:0] dg = (px0[10:5] >= bg_read[10:5]) ?
                    (px0[10:5] - bg_read[10:5]) :
                    (bg_read[10:5] - px0[10:5]);
    wire [5:0] db = (px0[4:0] >= bg_read[4:0]) ?
                    (px0[4:0] - bg_read[4:0]) :
                    (bg_read[4:0] - px0[4:0]);
    wire [6:0] color_distance = {1'b0, dr} + {1'b0, dg} + {1'b0, db};
    reg        v1, fg1;
    reg [11:0] x1, y1;
    always @(posedge pclk) begin
        if (rst_pclk) begin
            v1 <= 0; fg1 <= 0; x1 <= 0; y1 <= 0;
        end else begin
            v1  <= v0;
            x1  <= x0;
            y1  <= y0;
            fg1 <= !forced_white0 && (color_distance > COLOR_THRESHOLD);
        end
    end

    // S2/S3: two one-bit line memories and three-tap column shift registers.
    // A causal 3x3 majority vote removes isolated foreground pixels. It
    // delays the apparent outline by at most one pixel in x/y, while keeping
    // exactly WIDTH*HEIGHT output pixels per frame for the existing DDR path.
    (* ram_style = "distributed" *) reg row1 [0:WIDTH-1];
    (* ram_style = "distributed" *) reg row2 [0:WIDTH-1];
    reg row1_read, row2_read;
    reg v2, fg2;
    reg [11:0] x2, y2;
    always @(posedge pclk) begin
        if (rst_pclk) begin
            row1_read <= 0; row2_read <= 0;
            v2 <= 0; fg2 <= 0; x2 <= 0; y2 <= 0;
        end else begin
            v2 <= v1;
            fg2 <= fg1;
            x2 <= x1;
            y2 <= y1;
            if (v1) begin
                row1_read <= row1[x1];
                row2_read <= row2[x1];
            end
        end
    end

    reg [2:0] t0, t1, t2;
    reg foreground3;
    wire [2:0] n0 = (x2 == 0) ? {2'b00, fg2} : {t0[1:0], fg2};
    wire [2:0] n1 = (x2 == 0) ? {2'b00, row1_read} : {t1[1:0], row1_read};
    wire [2:0] n2 = (x2 == 0) ? {2'b00, row2_read} : {t2[1:0], row2_read};
    wire [3:0] votes = n0[0]+n0[1]+n0[2]+n1[0]+n1[1]+n1[2]+n2[0]+n2[1]+n2[2];
    always @(posedge pclk) begin
        if (rst_pclk) begin
            t0 <= 0; t1 <= 0; t2 <= 0;
            mask_valid <= 0;
            foreground3 <= 0;
        end else begin
            mask_valid <= v2;
            if (v2) begin
                row1[x2] <= fg2;
                row2[x2] <= row1_read;
                t0 <= n0;
                t1 <= n1;
                t2 <= n2;
                foreground3 <= ((x2 >= 2) && (y2 >= 2)) ? (votes >= 5) : fg2;
            end
        end
    end
    assign mask_rgb565 = foreground3 ? 16'h0000 : 16'hffff;
endmodule
