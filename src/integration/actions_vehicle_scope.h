/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file actions_vehicle_scope.h Bounded native consist and shared-order membership walks. */

#ifndef INTEGRATION_ACTIONS_VEHICLE_SCOPE_H
#define INTEGRATION_ACTIONS_VEHICLE_SCOPE_H

#include "actions_internal.h"
#include "actions_limits.h"
#include "../vehicle_base.h"

#include <algorithm>
#include <vector>

namespace Integration::Detail {

/** Native train length64 tiles and minimum part length1 imply at most1024 parts. */
inline std::vector<const Vehicle *> NativeConsistParts(const Vehicle *vehicle)
{
	std::vector<const Vehicle *> parts;
	for (const Vehicle *part = vehicle->First(); part != nullptr; part = part->Next()) {
		if (parts.size() == NATIVE_MAX_CONSIST_PARTS) throw InvalidParameters("Native consist exceeds the native train-length bound");
		if (part->owner != vehicle->owner || part->type != vehicle->type) throw InvalidParameters("Native consist membership is invalid");
		parts.push_back(part);
	}
	return parts;
}

/** A shared list contains primary vehicles of one owner/type, whose native limit is10000. */
inline std::vector<const Vehicle *> NativeSharedVehicles(const Vehicle *vehicle)
{
	std::vector<const Vehicle *> shared;
	for (const Vehicle *member = vehicle->FirstShared(); member != nullptr; member = member->NextShared()) {
		if (shared.size() == NATIVE_MAX_SHARED_VEHICLES) throw InvalidParameters("Shared-order cohort exceeds the 10000-vehicle native limit");
		if (!member->IsPrimaryVehicle() || member->owner != vehicle->owner || member->type != vehicle->type || member->orders != vehicle->orders) throw InvalidParameters("Invalid shared-order cohort");
		shared.push_back(member);
	}
	std::ranges::sort(shared, {}, [](const Vehicle *member) { return member->index.base(); });
	if (shared.empty() || std::ranges::adjacent_find(shared) != shared.end()) throw InvalidParameters("Invalid shared-order cohort");
	return shared;
}

} // namespace Integration::Detail

#endif /* INTEGRATION_ACTIONS_VEHICLE_SCOPE_H */
