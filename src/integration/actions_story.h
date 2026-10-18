/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 2.
 */

/** @file actions_story.h Normal GameScript player presentation and interaction seams. */

#ifndef INTEGRATION_ACTIONS_STORY_H
#define INTEGRATION_ACTIONS_STORY_H

#include "actions.h"
#include "../company_type.h"
#include "../goal_type.h"
#include "../network/network_type.h"
#include "../news_type.h"
#include "../story_type.h"
#include "../strings_type.h"

namespace Integration {

/** Execute-only hooks, before native local-player filtering. No command is emitted here. */
void StoryQuestionPresented(uint16_t id, uint32_t target, bool is_client, GoalQuestionButtons buttons, GoalQuestionType type, const EncodedString &text);
/** Exact selected issuance for a typed operation; absent means native newest-first close. */
void StoryQuestionAnswered(CompanyID company, uint16_t id, std::optional<uint64_t> selected_generation = std::nullopt);
void StoryQuestionsClosed(uint16_t id);
/** Observe one accepted Execute custom-news delivery, without decoding its body. */
void CustomNewsPresented(NewsType type, CompanyID company, const NewsReference &reference, const EncodedString &text);
/** A complete presentation reset clears rows, but never reuses an issuance in this engine boot. */
void ResetStoryPresentations();
uint64_t LastStoryQuestionGeneration();

namespace Detail {

class ActionInvocation;
std::optional<ActionResult> DispatchStoryAction(const std::string &action, const Json &parameters, ActionInvocation &invocation);
std::optional<Json> QueryStoryState(const std::string &query, const Json &parameters);
/** Exact current-recipient retained body; rendered only by the shared display.text path. */
const EncodedString &CustomNewsText(const Json &target);
std::optional<Json> CaptureStoryTargets(const std::string &action, const Json &parameters);
/** Resolve an explicitly selected interactive presentation; handles/tokens only. */
Json ResolveStoryControlReference(const std::string &kind, uint32_t id);
/** Local, readonly lease for an own/global native tile/vehicle button being armed. */
std::string StoryPlacementVersion(StoryPageElementID element);
/** Private immutable content witness from the actual clicked native popup. */
Json MakeStoryQuestionWitness(uint16_t id, GoalQuestionType type, GoalQuestionButtons buttons, const EncodedString &text);
/** Validate a bound renderer's captured popup witness, independent of the text inbox. */
ActionResult ValidateStoryQuestionWitness(const CommandDataBuffer &data, const Json &witness, ClientID owner);
/** owner is private, already authenticated native connection context; absent for text actors. */
std::optional<Json> StoryIntentState(Commands command, const CommandDataBuffer &data, ClientID owner = ClientID::Invalid);
std::optional<ActionResult> ValidateStoryNativeTargets(Commands command, const CommandDataBuffer &data, const TargetWatermarks &watermarks, ClientID owner = ClientID::Invalid);

} // namespace Detail
} // namespace Integration

#endif /* INTEGRATION_ACTIONS_STORY_H */
