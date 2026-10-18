/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file actions_limits.h Native gameplay bounds and fixed private transport budgets. */

#ifndef INTEGRATION_ACTIONS_LIMITS_H
#define INTEGRATION_ACTIONS_LIMITS_H

#include <cstddef>
#include <cstdint>

namespace Integration::Detail {

inline constexpr size_t NATIVE_MAX_STATION_EXTENT = 64;
inline constexpr size_t NATIVE_MAX_SHARED_VEHICLES = 10000;
inline constexpr size_t NATIVE_MAX_CONSIST_PARTS = 1024;
inline constexpr uint32_t NATIVE_MAX_SPATIAL_TILES = 1U << 24;
inline constexpr size_t NATIVE_MAX_COMMAND_BYTES = 8192;
inline constexpr size_t NATIVE_MAX_OPERATION_STATE_BYTES = 16384;
inline constexpr size_t NATIVE_MAX_BUNDLE_BYTES = 48000;

} // namespace Integration::Detail

#endif /* INTEGRATION_ACTIONS_LIMITS_H */
