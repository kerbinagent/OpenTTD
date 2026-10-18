/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file renderer.h Private, company-scoped rendering-worker interface. */

#ifndef INTEGRATION_RENDERER_H
#define INTEGRATION_RENDERER_H

#include "../3rdparty/nlohmann/json.hpp"
#include "bridge_hooks.h"

namespace Integration {

using Json = nlohmann::json;

/** Upper bound for an operator-selected lease, further reduced by native lag budgets. */
inline constexpr uint32_t MAX_RENDERER_PARK_MS = 40000;

/** Configure only from authenticated private IPC, on the game/main thread. */
void ConfigureRenderer(CompanyID company, const std::string &session, const std::string &authority_instance = {}, const std::string &world_epoch = {}, uint32_t authority_max_lag_ticks = 0);
/** Invalidate screenshot targets on replica disconnect or map replacement. */
void RendererInvalidateSnapshot();
bool RendererShouldPark();
void RendererTick();

/** Capture native commands only while a bounded, screenshot-bound gesture is active. */
void RendererBeginOwnedCommand(Commands command, const CommandDataBuffer &data, bool my_command, uint8_t callback);
void RendererEndOwnedCommand();

/**
 * Private IPC only: bounded UI controls, parked gestures and immutable PNG artifacts.
 * Capture metadata includes a private filesystem path which must never be exposed.
 * Run on the game/main thread. Captured gestures never execute gameplay locally.
 */
Json RendererControl(const Json &params);
Json RendererCapture(const Json &params);
/** Read-only, nonparked observation artifacts; never usable as screenshot/input proofs. */
Json RendererObserveWorld(const Json &params);
Json RendererObserveNativeInfo(const Json &params);
/** Reset the one-new-observation-work-item budget at the start of private IPC Tick. */
void RendererBeginObservationTick();
Json RendererUICapture(const Json &params);
Json RendererUIStatus(const Json &params);
Json RendererUIRelease(const Json &params);

} // namespace Integration

#endif /* INTEGRATION_RENDERER_H */
