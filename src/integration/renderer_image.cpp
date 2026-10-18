/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file renderer_image.cpp Metadata-free screen image encoder. */

#include "../stdafx.h"
#include "renderer_image.h"

#ifdef WITH_PNG
#include <png.h>
#endif

#include "../safeguards.h"

namespace Integration {

namespace {

constexpr uint32_t MAX_IMAGE_DIMENSION = 4096;

#ifdef WITH_PNG

/** Heap storage remains valid across libpng's error longjmp. */
struct PngOutput {
	std::vector<uint8_t> bytes;
	std::array<char, 256> error{};
};

static void PNGAPI PngError(png_structp png, png_const_charp message)
{
	auto &output = *static_cast<PngOutput *>(png_get_error_ptr(png));
	if (message != nullptr) {
		const size_t length = std::min(std::char_traits<char>::length(message), output.error.size() - 1);
		std::copy_n(message, length, output.error.data());
		output.error[length] = '\0';
	}
	png_longjmp(png, 1);
}

static void PNGAPI PngWarning(png_structp, png_const_charp)
{
}

static void PNGAPI WritePngData(png_structp png, png_bytep data, png_size_t length)
{
	auto &output = *static_cast<PngOutput *>(png_get_io_ptr(png));
	constexpr size_t MAX_ENCODED_BYTES = 64 * 1024 * 1024;
	if (length > MAX_ENCODED_BYTES - output.bytes.size()) png_error(png, "Encoded image exceeds size limit");

	bool allocation_failed = false;
	try {
		output.bytes.insert(output.bytes.end(), data, data + length);
	} catch (...) {
		allocation_failed = true;
	}
	/* Leave the catch scope before longjmp so exception cleanup has finished. */
	if (allocation_failed) png_error(png, "Unable to allocate encoded image");
}

static void PNGAPI FlushPngData(png_structp)
{
}

static RendererImage EncodePng(const std::vector<uint8_t> &rgb, uint32_t width, uint32_t height)
{
	/* No nontrivial automatic objects are created while libpng may longjmp. */
	auto output = std::make_unique<PngOutput>();
	png_structp png = png_create_write_struct(PNG_LIBPNG_VER_STRING, output.get(), PngError, PngWarning);
	if (png == nullptr) throw std::runtime_error("Unable to create PNG encoder");
	png_infop info = png_create_info_struct(png);
	if (info == nullptr) {
		png_destroy_write_struct(&png, nullptr);
		throw std::runtime_error("Unable to create PNG image header");
	}

	if (setjmp(png_jmpbuf(png)) != 0) {
		png_destroy_write_struct(&png, &info);
		throw std::runtime_error(output->error[0] != '\0' ? output->error.data() : "PNG encoding failed");
	}

	png_set_write_fn(png, output.get(), WritePngData, FlushPngData);
	png_set_IHDR(png, info, width, height, 8, PNG_COLOR_TYPE_RGB, PNG_INTERLACE_NONE,
		PNG_COMPRESSION_TYPE_DEFAULT, PNG_FILTER_TYPE_DEFAULT);
	png_set_compression_level(png, 3);
	/* Only IHDR, IDAT and IEND are emitted. Never call png_set_text or copy savegame metadata. */
	png_write_info(png, info);
	for (uint32_t y = 0; y < height; ++y) {
		png_write_row(png, rgb.data() + static_cast<size_t>(y) * width * 3);
	}
	png_write_end(png, info);
	png_destroy_write_struct(&png, &info);
	return {"image/png", std::move(output->bytes)};
}

#else

static void WriteLittleEndian(std::vector<uint8_t> &bytes, size_t offset, uint32_t value, size_t count)
{
	for (size_t i = 0; i < count; ++i) bytes[offset + i] = static_cast<uint8_t>(value >> (i * 8));
}

static RendererImage EncodeBmp(const std::vector<uint8_t> &rgb, uint32_t width, uint32_t height)
{
	const size_t row_size = (static_cast<size_t>(width) * 3 + 3) & ~size_t{3};
	const size_t pixel_bytes = row_size * height;
	constexpr size_t HEADER_SIZE = 54;
	RendererImage image{"image/bmp", std::vector<uint8_t>(HEADER_SIZE + pixel_bytes, 0)};
	image.bytes[0] = 'B';
	image.bytes[1] = 'M';
	WriteLittleEndian(image.bytes, 2, static_cast<uint32_t>(image.bytes.size()), 4);
	WriteLittleEndian(image.bytes, 10, HEADER_SIZE, 4);
	WriteLittleEndian(image.bytes, 14, 40, 4); // BITMAPINFOHEADER size.
	WriteLittleEndian(image.bytes, 18, width, 4);
	WriteLittleEndian(image.bytes, 22, height, 4);
	WriteLittleEndian(image.bytes, 26, 1, 2); // One colour plane.
	WriteLittleEndian(image.bytes, 28, 24, 2); // 24 bits per pixel, no compression.
	WriteLittleEndian(image.bytes, 34, static_cast<uint32_t>(pixel_bytes), 4);
	for (uint32_t y = 0; y < height; ++y) {
		const size_t source_row = static_cast<size_t>(y) * width * 3;
		const size_t target_row = HEADER_SIZE + static_cast<size_t>(height - y - 1) * row_size;
		for (uint32_t x = 0; x < width; ++x) {
			const size_t source = source_row + static_cast<size_t>(x) * 3;
			const size_t target = target_row + static_cast<size_t>(x) * 3;
			image.bytes[target] = rgb[source + 2];
			image.bytes[target + 1] = rgb[source + 1];
			image.bytes[target + 2] = rgb[source];
		}
	}
	return image;
}

#endif /* WITH_PNG */

} // namespace

RendererImage EncodeRendererImage(const std::vector<uint8_t> &rgb, uint32_t width, uint32_t height)
{
	/* Validate before multiplication, including on 32-bit platforms. */
	if (width == 0 || height == 0 || width > MAX_IMAGE_DIMENSION || height > MAX_IMAGE_DIMENSION) {
		throw std::invalid_argument("Renderer image dimensions must be between 1 and 4096");
	}
	const size_t expected_size = static_cast<size_t>(width) * height * 3;
	if (rgb.size() != expected_size) throw std::invalid_argument("Renderer image RGB buffer has incorrect size");

#ifdef WITH_PNG
	return EncodePng(rgb, width, height);
#else
	return EncodeBmp(rgb, width, height);
#endif
}

} // namespace Integration
