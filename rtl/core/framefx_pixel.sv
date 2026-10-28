module framefx_pixel (
    input logic [1:0] mode,
    input logic [7:0] threshold,
    input logic [7:0] center,
    input logic [7:0] north_left, north_middle, north_right,
    input logic [7:0] middle_left, middle_right,
    input logic [7:0] south_left, south_middle, south_right,
    output logic [7:0] pixel
);
    logic signed [11:0] gradient_x, gradient_y;
    logic [11:0] absolute_x, absolute_y;
    logic [12:0] magnitude;

    always_comb begin
        gradient_x = -$signed({4'b0, north_left}) + $signed({4'b0, north_right})
                   - ($signed({4'b0, middle_left}) <<< 1) + ($signed({4'b0, middle_right}) <<< 1)
                   - $signed({4'b0, south_left}) + $signed({4'b0, south_right});
        gradient_y = -$signed({4'b0, north_left}) - ($signed({4'b0, north_middle}) <<< 1)
                   - $signed({4'b0, north_right}) + $signed({4'b0, south_left})
                   + ($signed({4'b0, south_middle}) <<< 1) + $signed({4'b0, south_right});
        absolute_x = gradient_x < 0 ? $unsigned(-gradient_x) : $unsigned(gradient_x);
        absolute_y = gradient_y < 0 ? $unsigned(-gradient_y) : $unsigned(gradient_y);
        magnitude = ({1'b0, absolute_x} + {1'b0, absolute_y}) >> 2;
        case (mode)
            0: pixel = center;
            1: pixel = ~center;
            2: pixel = center >= threshold ? 8'hff : 8'h00;
            default: pixel = magnitude > 255 ? 8'hff : magnitude[7:0];
        endcase
    end
endmodule
