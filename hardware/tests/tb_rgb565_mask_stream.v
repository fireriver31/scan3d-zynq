`timescale 1ns / 1ps

module tb_rgb565_mask_stream;
    localparam W = 16;
    localparam H = 8;
    reg clk = 0;
    always #5 clk = ~clk;
    reg rst = 1;
    reg vs = 0;
    reg valid = 0;
    reg [15:0] rgb = 0;
    reg [11:0] x = 0, y = 0;
    wire mask_valid, background_ready;
    wire [15:0] mask_rgb;
    integer xx, yy, frame, seen = 0;
    integer out_x, out_y;
    integer errors = 0;

    rgb565_mask_stream #(
        .WIDTH(W), .HEIGHT(H), .BG_X_STEP(2), .BG_Y_STEP(2),
        .COLOR_THRESHOLD(18)
    ) dut (
        .pclk(clk), .rst(rst), .frame_vs(vs), .pixel_valid(valid),
        .pixel_rgb565(rgb), .pixel_x_one_based(x),
        .pixel_y_one_based(y), .mask_valid(mask_valid),
        .mask_rgb565(mask_rgb), .background_ready(background_ready)
    );

    always @(posedge clk) begin
        #1;
        if (mask_valid) begin
            out_x = (seen % (W*H)) % W;
            out_y = (seen % (W*H)) / W;
            if (seen < W*H && mask_rgb !== 16'hffff) begin
                $display("FAIL: reference frame pixel %0d is not white", seen);
                errors = errors + 1;
            end
            // A 5x5 solid object must survive the 3x3 majority filter.
            if (seen == W*H + 4*W + 6 && mask_rgb !== 16'h0000) begin
                $display("FAIL: solid object was removed");
                errors = errors + 1;
            end
            // One isolated changed pixel must disappear.
            if (seen == W*H + 6*W + 12 && mask_rgb !== 16'hffff) begin
                $display("FAIL: isolated pixel was not removed");
                errors = errors + 1;
            end
            if (seen >= W*H &&
                (out_x < 4 || out_x > 10 || out_y < 2) &&
                mask_rgb !== 16'hffff) begin
                $display("FAIL: textured background at (%0d,%0d) became foreground", out_x, out_y);
                errors = errors + 1;
            end
            seen = seen + 1;
        end
    end

    initial begin
        repeat (3) @(negedge clk);
        rst = 0;
        repeat (4) @(negedge clk); // allow the PCLK reset synchronizer to release
        for (frame = 0; frame < 2; frame = frame + 1) begin
            @(negedge clk); vs = 1;
            @(negedge clk); vs = 0;
            for (yy = 0; yy < H; yy = yy + 1) begin
                for (xx = 0; xx < W; xx = xx + 1) begin
                    @(negedge clk);
                    x = xx + 1; y = yy + 1; valid = 1;
                    // High-contrast, nonuniform empty scene: each 2x2 cell
                    // must still compare to its own captured reference.
                    rgb = (((xx/2 + yy/2) % 2) == 0) ? 16'hffff : 16'h07ff;
                    if (frame == 1 &&
                        (((xx >= 4 && xx <= 8) && (yy >= 2 && yy <= 6)) ||
                          (xx == 12 && yy == 6))) rgb = 16'h0000;
                    @(negedge clk); valid = 0;
                end
            end
            repeat (8) @(negedge clk);
        end
        repeat (8) @(negedge clk);
        if (!background_ready || seen != 2*W*H || errors != 0) begin
            $display("FAIL: ready=%0d pixels=%0d expected=%0d errors=%0d",
                     background_ready, seen, 2*W*H, errors);
            $fatal(1);
        end
        $display("PASS: background capture, color comparison, 3x3 denoise, pixel count");
        $finish;
    end
endmodule
