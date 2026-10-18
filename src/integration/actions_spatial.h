/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file actions_spatial.h Compact, map-bounded native spatial proof. */

#ifndef INTEGRATION_ACTIONS_SPATIAL_H
#define INTEGRATION_ACTIONS_SPATIAL_H

#include "actions.h"
#include "../tile_type.h"

#include <iterator>
#include <vector>

namespace Integration::Detail {

/** Unique tile indexes, visited in ascending order; at most two MiB on a native map. */
class NativeTileSet {
	std::vector<uint64_t> words;
	uint32_t count = 0;
public:
	class Iterator {
		const std::vector<uint64_t> *words;
		size_t word;
		uint64_t remaining;
		void SkipEmpty();
	public:
		using value_type = TileIndex;
		using difference_type = std::ptrdiff_t;
		using iterator_category = std::forward_iterator_tag;
		using pointer = void;
		using reference = TileIndex;
		Iterator(const std::vector<uint64_t> &words, size_t word);
		TileIndex operator*() const;
		Iterator &operator++();
		Iterator operator++(int) { auto previous = *this; ++*this; return previous; }
		bool operator==(const Iterator &other) const { return this->words == other.words && this->word == other.word && this->remaining == other.remaining; }
	};

	void insert(TileIndex tile);
	void clear();
	size_t size() const { return this->count; }
	Iterator begin() const { return {this->words, 0}; }
	Iterator end() const { return {this->words, this->words.size()}; }
};

/** Fixed BLAKE2b-256 protocol v2: command, count, ordered index and tile bytes except Clear's invisible growth counter. */
Json NativeSpatialProof(Commands command, const CommandDataBuffer &data, const NativeTileSet &tiles);

} // namespace Integration::Detail

#endif /* INTEGRATION_ACTIONS_SPATIAL_H */
