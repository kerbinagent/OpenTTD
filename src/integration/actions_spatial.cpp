/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file actions_spatial.cpp Compact native geometry membership and meaningful tile commitment. */

#include "../stdafx.h"
#include "actions_spatial.h"
#include "actions_internal.h"
#include "actions_limits.h"
#include "../3rdparty/monocypher/monocypher.h"
#include "../map_func.h"
#include "../tile_map.h"

#include <bit>

#include "../safeguards.h"

namespace Integration::Detail {

NativeTileSet::Iterator::Iterator(const std::vector<uint64_t> &words, size_t word) : words(&words), word(word), remaining(word < words.size() ? words[word] : 0)
{
	this->SkipEmpty();
}

void NativeTileSet::Iterator::SkipEmpty()
{
	while (this->remaining == 0 && this->word < this->words->size()) {
		++this->word;
		if (this->word < this->words->size()) this->remaining = (*this->words)[this->word];
	}
}

TileIndex NativeTileSet::Iterator::operator*() const
{
	return TileIndex(static_cast<uint32_t>(this->word * 64 + std::countr_zero(this->remaining)));
}

NativeTileSet::Iterator &NativeTileSet::Iterator::operator++()
{
	this->remaining &= this->remaining - 1;
	this->SkipEmpty();
	return *this;
}

void NativeTileSet::insert(TileIndex tile)
{
	if (Map::Size() > NATIVE_MAX_SPATIAL_TILES || tile >= Map::Size()) throw InvalidParameters("Native intent contains an invalid tile");
	if (this->words.empty()) this->words.resize((Map::Size() + 63) / 64);
	auto &word = this->words[tile.base() / 64];
	uint64_t mask = uint64_t{1} << (tile.base() % 64);
	if ((word & mask) == 0) { word |= mask; ++this->count; }
}

void NativeTileSet::clear()
{
	std::ranges::fill(this->words, 0);
	this->count = 0;
}

Json NativeSpatialProof(Commands command, const CommandDataBuffer &data, const NativeTileSet &tiles)
{
	if (data.size() > NATIVE_MAX_COMMAND_BYTES || tiles.size() > Map::Size()) throw InvalidParameters("Native spatial proof exceeds engine bounds");
	crypto_blake2b_ctx hash;
	crypto_blake2b_init(&hash, 32);
	auto bytes = [&](std::span<const uint8_t> value) { crypto_blake2b_update(&hash, value.data(), value.size()); };
	auto integer = [&](uint32_t value) {
		std::array<uint8_t, 4> encoded;
		for (uint shift = 0; shift < encoded.size(); ++shift) encoded[shift] = static_cast<uint8_t>(value >> (8 * shift));
		bytes(encoded);
	};
	static constexpr std::string_view DOMAIN = "OpenTTD integration spatial proof v2 clear-counter-normalized";
	bytes({reinterpret_cast<const uint8_t *>(DOMAIN.data()), DOMAIN.size()});
	integer(static_cast<uint32_t>(command));
	integer(static_cast<uint32_t>(data.size()));
	bytes(data);
	integer(static_cast<uint32_t>(tiles.size()));
	for (TileIndex index : tiles) {
		integer(index.base());
		Tile tile(index);
		/* GetClearCounter is an invisible tile-loop phase, read only by
		 * TileLoop_Clear. Keep ground type/density and every other byte: these
		 * determine native clearing cost, geometry, ownership and assets. */
		const uint8_t m5 = IsTileType(tile, TileType::Clear) ? tile.m5() & 0x1F : tile.m5();
		const std::array<uint8_t, 12> record = {tile.type(), tile.height(), tile.m1(), static_cast<uint8_t>(tile.m2()), static_cast<uint8_t>(tile.m2() >> 8),
			tile.m3(), tile.m4(), m5, tile.m6(), tile.m7(), static_cast<uint8_t>(tile.m8()), static_cast<uint8_t>(tile.m8() >> 8)};
		bytes(record);
	}
	std::array<uint8_t, 32> digest;
	crypto_blake2b_final(&hash, digest.data());
	static constexpr char HEX[] = "0123456789abcdef";
	std::string hex; hex.reserve(digest.size() * 2);
	for (uint8_t byte : digest) { hex.push_back(HEX[byte >> 4]); hex.push_back(HEX[byte & 15]); }
	return {{"count", tiles.size()}, {"digest", std::move(hex)}};
}

} // namespace Integration::Detail
