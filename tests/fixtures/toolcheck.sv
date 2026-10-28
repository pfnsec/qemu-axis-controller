module toolcheck (
    input  logic       clk,
    input  logic       reset_n,
    input  logic       enable,
    output logic [7:0] count
);
    always_ff @(posedge clk) begin
        if (!reset_n)
            count <= 8'd0;
        else if (enable)
            count <= count + 8'd1;
    end
endmodule
