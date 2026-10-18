/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 2.
 */

/** @file integration_bridge.h Private control plane for one world or renderer. */

#ifndef INTEGRATION_BRIDGE_H
#define INTEGRATION_BRIDGE_H

#include "../3rdparty/nlohmann/json.hpp"
#include "bridge_hooks.h"

#include <optional>

namespace Integration {

using Json = nlohmann::json;

/** Simulation-thread event publication. Private consumers must project recipient and mode ACLs. */
void PublishEvent(const Json &event);

/** Opaque boot identity used by private protocol fences and public entity handles. */
std::string_view InstanceId();

/** Consume normal prequeue native test feedback for the active typed submission. */
std::optional<Json> TakeSubmittedPostFailure();

} // namespace Integration

#endif /* INTEGRATION_BRIDGE_H */
