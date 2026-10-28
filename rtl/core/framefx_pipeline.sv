module framefx_pipeline (
    input logic clk, reset_n, start,
    input logic [63:0] source, destination,
    input logic [10:0] width, height,
    input logic [31:0] source_stride, destination_stride,
    input logic [1:0] mode,
    input logic [7:0] threshold,
    output logic done,
    output logic [2:0] error_code,
    output logic command_valid, command_write,
    input logic command_ready,
    output logic [63:0] command_address,
    output logic [10:0] command_bytes,
    input logic dma_done,
    input logic [2:0] dma_error,
    input logic read_valid,
    input logic [63:0] read_data,
    output logic write_valid,
    output logic [63:0] write_data,
    input logic write_ready
);
    typedef enum logic [3:0] {IDLE, ENSURE_ROWS, READ_COMMAND, READ_ROW, FETCH_WORD,
                             SCAN, FLUSH, WRITE_COMMAND, WRITE_ROW, FINISH} state_t;
    state_t state;
    logic [63:0] line_zero [0:127];
    logic [63:0] line_one [0:127];
    logic [63:0] line_two [0:127];
    logic [63:0] output_line [0:127];
    logic [63:0] word_zero, word_one, word_two, packed_word, next_packed;
    logic [63:0] read_cursor, write_cursor;
    logic [10:0] columns, rows, output_row, loaded_rows, column_index, sent_words;
    logic [31:0] read_stride, write_stride;
    logic [1:0] operation, load_slot, center_slot;
    logic [7:0] cutoff, north_current, middle_current, south_current;
    logic [7:0] north_left, north_middle, middle_left, middle_middle, south_left, south_middle;
    logic [7:0] filtered, output_pixel;
    logic [6:0] load_word, pack_index;
    logic [2:0] pack_lane;
    logic output_valid;

    assign done = state == FINISH;
    assign command_valid = state == READ_COMMAND || state == WRITE_COMMAND;
    assign command_write = state == WRITE_COMMAND;
    assign command_address = command_write ? write_cursor : read_cursor;
    assign command_bytes = command_write ? columns : ((columns + 7) & 11'h7f8);
    assign write_valid = state == WRITE_ROW && sent_words < ((columns + 7) >> 3);
    assign center_slot = 2'(output_row % 3);

    always_comb begin
        case (center_slot)
            0: begin north_current = word_two[7:0]; middle_current = word_zero[7:0]; south_current = word_one[7:0]; end
            1: begin north_current = word_zero[7:0]; middle_current = word_one[7:0]; south_current = word_two[7:0]; end
            default: begin north_current = word_one[7:0]; middle_current = word_two[7:0]; south_current = word_zero[7:0]; end
        endcase
        output_valid = (state == SCAN && column_index != 0) || state == FLUSH;
        output_pixel = filtered;
        if (operation == 3 && (output_row == 0 || output_row + 1 == rows || column_index == 1 || state == FLUSH))
            output_pixel = 0;
        next_packed = packed_word;
        next_packed[pack_lane * 8 +: 8] = output_pixel;
    end

    framefx_pixel filter (
        .mode(operation), .threshold(cutoff), .center(middle_middle),
        .north_left, .north_middle, .north_right(north_current),
        .middle_left, .middle_right(middle_current),
        .south_left, .south_middle, .south_right(south_current), .pixel(filtered)
    );

    always_ff @(posedge clk) begin
        if (!reset_n) begin
            state <= IDLE;
            columns <= 0; rows <= 0; output_row <= 0; loaded_rows <= 0; column_index <= 0; sent_words <= 0;
            read_cursor <= 0; write_cursor <= 0; read_stride <= 0; write_stride <= 0;
            operation <= 0; cutoff <= 0; load_slot <= 0; load_word <= 0; pack_index <= 0; pack_lane <= 0;
            packed_word <= 0; word_zero <= 0; word_one <= 0; word_two <= 0; write_data <= 0;
            north_left <= 0; north_middle <= 0; middle_left <= 0; middle_middle <= 0; south_left <= 0; south_middle <= 0;
            error_code <= 0;
        end else begin
            if (state == READ_ROW && read_valid) begin
                case (load_slot)
                    0: line_zero[load_word] <= read_data;
                    1: line_one[load_word] <= read_data;
                    default: line_two[load_word] <= read_data;
                endcase
                load_word <= load_word + 1'b1;
            end
            if (output_valid) begin
                packed_word <= next_packed;
                pack_lane <= pack_lane + 1'b1;
                if (pack_lane == 7 || state == FLUSH) begin
                    output_line[pack_index] <= next_packed;
                    pack_index <= pack_index + 1'b1;
                    packed_word <= 0;
                end
            end
            case (state)
                IDLE: if (start) begin
                    columns <= width; rows <= height; operation <= mode; cutoff <= threshold;
                    read_cursor <= source; write_cursor <= destination;
                    read_stride <= source_stride; write_stride <= destination_stride;
                    output_row <= 0; loaded_rows <= 0; load_slot <= 0;
                    error_code <= 0;
                    state <= ENSURE_ROWS;
                end
                ENSURE_ROWS: begin
                    if (loaded_rows < rows && loaded_rows <= output_row + (operation == 3 ? 11'd1 : 11'd0))
                        state <= READ_COMMAND;
                    else begin
                        column_index <= 0; pack_index <= 0; pack_lane <= 0; packed_word <= 0;
                        north_left <= 0; north_middle <= 0; middle_left <= 0; middle_middle <= 0; south_left <= 0; south_middle <= 0;
                        state <= FETCH_WORD;
                    end
                end
                READ_COMMAND: if (command_ready) begin load_word <= 0; state <= READ_ROW; end
                READ_ROW: if (dma_done) begin
                    if (dma_error != 0) begin error_code <= dma_error; state <= FINISH; end
                    else begin
                        loaded_rows <= loaded_rows + 1'b1;
                        load_slot <= load_slot == 2 ? 0 : load_slot + 1'b1;
                        read_cursor <= read_cursor + {32'b0, read_stride};
                        state <= ENSURE_ROWS;
                    end
                end
                FETCH_WORD: begin
                    word_zero <= line_zero[column_index[9:3]];
                    word_one <= line_one[column_index[9:3]];
                    word_two <= line_two[column_index[9:3]];
                    state <= SCAN;
                end
                SCAN: begin
                    north_left <= north_middle; north_middle <= north_current;
                    middle_left <= middle_middle; middle_middle <= middle_current;
                    south_left <= south_middle; south_middle <= south_current;
                    word_zero <= word_zero >> 8; word_one <= word_one >> 8; word_two <= word_two >> 8;
                    if (column_index + 1 == columns) state <= FLUSH;
                    else begin
                        column_index <= column_index + 1'b1;
                        if (column_index[2:0] == 7) state <= FETCH_WORD;
                    end
                end
                FLUSH: state <= WRITE_COMMAND;
                WRITE_COMMAND: if (command_ready) begin
                    sent_words <= 0;
                    write_data <= output_line[0];
                    state <= WRITE_ROW;
                end
                WRITE_ROW: begin
                    if (write_valid && write_ready) begin
                        sent_words <= sent_words + 1'b1;
                        if (sent_words + 1 < ((columns + 7) >> 3)) write_data <= output_line[7'(sent_words + 1)];
                    end
                    if (dma_done) begin
                        if (dma_error != 0) begin error_code <= dma_error; state <= FINISH; end
                        else if (output_row + 1 == rows) state <= FINISH;
                        else begin
                            output_row <= output_row + 1'b1;
                            write_cursor <= write_cursor + {32'b0, write_stride};
                            state <= ENSURE_ROWS;
                        end
                    end
                end
                FINISH: state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end

`ifndef SYNTHESIS
    assert property (@(posedge clk) disable iff (!reset_n)
        state == SCAN |-> column_index < columns && columns <= 1024);
    assert property (@(posedge clk) disable iff (!reset_n)
        write_valid && !write_ready && !dma_done |=> write_valid && $stable(write_data));
`endif
endmodule
