/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file renderer_image.h Metadata-free encoding of renderer screen pixels. */

#ifndef RENDERER_IMAGE_H
#define RENDERER_IMAGE_H

#include <cstdint>
#include <string>
#include <vector>

namespace Integration {

/** An encoded screen image containing no game state or identifying metadata. */
struct RendererImage {
	std::string content_type;
	std::vector<uint8_t> bytes;
};

/**
 * Encode copied screen pixels without inspecting game state.
 *
 * Input is tightly packed RGB, ordered left to right, then top to bottom.
 * Width and height must each be between 1 and 4096 inclusive, and the input
 * must contain exactly width * height * 3 bytes. Output is PNG when WITH_PNG
 * is enabled, or a 24-bit BMP otherwise. Neither format includes metadata
 * beyond the image format and dimensions.
 *
 * @throws std::invalid_argument for invalid dimensions or pixel buffer size.
 * @throws std::runtime_error if the PNG encoder fails.
 * @throws std::bad_alloc if allocating the image buffer fails.
 */
RendererImage EncodeRendererImage(const std::vector<uint8_t> &rgb, uint32_t width, uint32_t height);

} // namespace Integration

#endif /* RENDERER_IMAGE_H */
