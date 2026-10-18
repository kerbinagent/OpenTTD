/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file renderer_observation.h Bounded ordinary native observation drawing. */

#ifndef INTEGRATION_RENDERER_OBSERVATION_H
#define INTEGRATION_RENDERER_OBSERVATION_H

#include "../viewport_type.h"
#include <cstdint>
#include <vector>

struct Window;

namespace Integration {

inline constexpr uint32_t MAX_OBSERVATION_WIDTH = 1280;
inline constexpr uint32_t MAX_OBSERVATION_HEIGHT = 1024;
inline constexpr uint64_t MAX_OBSERVATION_PIXELS = 1024 * 1024;
inline constexpr uint64_t MAX_OBSERVATION_VIRTUAL_AREA = 128 * 1024 * 1024;

struct ObservationLabels {
	bool towns;
	bool stations;
	bool waypoints;
	bool signs;
	bool competitors;
};

/** Copied native pixels, independent of any ongoing screen/input lease. */
struct ObservationPixels {
	uint32_t width;
	uint32_t height;
	std::vector<uint8_t> rgb;
};

/** Game thread only. Neither function changes the desktop camera or input proof. */
ObservationPixels DrawWorldObservation(const Viewport &viewport, const ObservationLabels &labels);
/** Only the two explicitly prepared, viewport-free information panels are supported. */
ObservationPixels DrawNativePanelObservation(Window &window);

} // namespace Integration

#endif /* INTEGRATION_RENDERER_OBSERVATION_H */
