"use strict";

const FramefxReference = (() => {
    function transform(source, width, height, mode, threshold) {
        if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 ||
            width > 1024 || height > 1024 || source.length !== width * height ||
            !Number.isInteger(mode) || mode < 0 || mode > 3 || !Number.isInteger(threshold) || threshold < 0 || threshold > 255 ||
            (mode === 3 && (width < 3 || height < 3))) throw new Error("Invalid reference geometry/mode");
        const output = new Uint8Array(source.length);
        for (let row = 0; row < height; row++) {
            for (let column = 0; column < width; column++) {
                const offset = row * width + column;
                if (mode === 0) output[offset] = source[offset];
                else if (mode === 1) output[offset] = 255 - source[offset];
                else if (mode === 2) output[offset] = source[offset] >= threshold ? 255 : 0;
                else if (row > 0 && row < height - 1 && column > 0 && column < width - 1) {
                    const top = offset - width, bottom = offset + width;
                    const horizontal = -source[top - 1] + source[top + 1] - 2 * source[offset - 1] +
                        2 * source[offset + 1] - source[bottom - 1] + source[bottom + 1];
                    const vertical = -source[top - 1] - 2 * source[top] - source[top + 1] +
                        source[bottom - 1] + 2 * source[bottom] + source[bottom + 1];
                    output[offset] = Math.min(255, (Math.abs(horizontal) + Math.abs(vertical)) >> 2);
                }
            }
        }
        return output;
    }
    return {transform};
})();

if (typeof module !== "undefined") module.exports = FramefxReference;
