/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 2.
 */

/** @file bridge_hooks.h Small integration hooks shared with the native command machinery. */

#ifndef INTEGRATION_BRIDGE_HOOKS_H
#define INTEGRATION_BRIDGE_HOOKS_H

#include "../command_type.h"
#include "../company_type.h"

#include <optional>

namespace Integration {

void Tick();
bool Enabled();
/** Initialization-safe intent check; does not initialize sockets or the ledger. */
bool TracksPlayerPresentations();
bool RendererIsActive();
bool RendererCaptureCommandIntent(Commands command, StringID error, CompanyID company, const CommandDataBuffer &data);
bool RendererCaptureNativeCommand(Commands command, StringID error, CommandCallback *callback, CompanyID company, const CommandDataBuffer &data);

/** Server-local correlation only. Never serialized to native clients. */
uint64_t SubmissionToken();
void CommandQueued(uint64_t token);
void CommandPostTestFailed(Commands command, const CommandCost &cost);
bool ValidateQueuedCommand(uint64_t token);
void BeginCommand(uint64_t token);
void EndCommand();
/** Exact text presentation selected by the executing request; no new answer state. */
std::optional<uint64_t> ExecutingQuestionGeneration(CompanyID company, uint16_t question_id);
void CommandCompleted(Commands command, const CommandCost &cost, const CommandDataBuffer &returned);

/** Main-thread save/load lifecycle evidence; never called for a load check. */
void WorldLoaded(std::string_view filename);
void SaveFinished(bool success);

} // namespace Integration

#endif /* INTEGRATION_BRIDGE_HOOKS_H */
