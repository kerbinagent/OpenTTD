/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 2.
 */

/** @file visual_commands.h Private, screenshot-bound native command forwarding. */

#ifndef INTEGRATION_VISUAL_COMMANDS_H
#define INTEGRATION_VISUAL_COMMANDS_H

#include "actions.h"
#include "../company_type.h"
#include "../network/network_type.h"
#include <optional>

namespace Integration {

/** Existing native callback registry; these helpers never add a wire callback. */
std::optional<uint8_t> NativeCallbackIndex(Commands command, CommandCallback *callback);
bool NormalizeNativeCommand(Commands command, uint8_t callback, CommandDataBuffer &data, ClientID owner);
bool QueueVisualNativeCommand(Commands command, StringID error, uint8_t callback, CompanyID company,
	const CommandDataBuffer &data, ClientID owner, uint64_t token);
/** Main-thread size of the operator's native wait and execution queues. */
size_t PendingLocalCommands();

/** Immutable private intent; never expose native bytes or identity hints publicly. */
struct VisualCommand {
	Json bundle;
	Commands command{};
	StringID error{};
	CompanyID company = CompanyID::Invalid();
	ClientID owner = ClientID::Invalid;
	uint8_t callback = 0;
	CommandDataBuffer data;
	bool ui_only = false;
};

/** Record pool watermarks after all commands and simulation work for this native frame. */
void CompletedFrame();

/** Check a trusted renderer's one-shot captured bundle against the authoritative world. */
ActionResult PrepareVisualCommand(const Json &bundle, const Json &actor, std::string_view request_id, VisualCommand &prepared);
/** Recheck the same frame/owner/target lease immediately before first-in-frame publication. */
ActionResult ValidateVisualCommand(const VisualCommand &prepared);

} // namespace Integration

#endif /* INTEGRATION_VISUAL_COMMANDS_H */
