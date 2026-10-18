/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file chat.h Bounded native chat ingress and exact-recipient delivery. */

#ifndef INTEGRATION_CHAT_H
#define INTEGRATION_CHAT_H

#include "../3rdparty/nlohmann/json.hpp"
#include "../network/network_type.h"

namespace Integration {

using Json = nlohmann::json;

/**
 * Consume native conversation before stock delivery when integration is enabled.
 * Only authenticated intent is retained; backend admission owns all conversation
 * ACLs, moderation, durable history and recipient selection. When disabled this
 * returns false and leaves stock delivery unchanged. Call on the game thread.
 */
bool InterceptNativeChat(NetworkAction action, NetworkChatDestinationType destination, int target,
		std::string_view body, ClientID sender, bool from_admin = false);

/**
 * Primary, reliable ingress channel. {limit:1..64,max_bytes:2048..49152} returns
 * oldest unacknowledged chat.received envelopes within both limits. Pending
 * storage is bounded to 256 messages/256 KiB; admitted records are never evicted.
 * Private chat bodies are never copied into the general engine event stream.
 */
Json ChatPending(const Json &request);

/**
 * {ingress:[{message_id,accepted:bool}]}.
 * Backend acknowledges ingress only after durable admission/rejection; a rejected
 * record produces generic requester-only feedback.
 */
Json ChatAcknowledge(const Json &request);

/**
 * Exact authorized native delivery, never company routing or ACL expansion.
 * {delivery_request_id:string,message_id,body,
 *  sender:{label,kind:"human"|"agent",company_id:int|null},
 *  recipient_public_keys:[authenticated public keys]}.
 * Call only after the root RequestLedger has admitted and deduplicated the
 * mutation envelope. This projection has no competing delivery cache or ACK.
 * Root records authoritative completion immediately after this function returns.
 * Native packets carry a sanitized sender prefix, not durable thread metadata.
 */
Json DeliverChat(const Json &request);

/** Authenticated native key/current-company facts for backend identity binding. */
Json NativeRoster();

} // namespace Integration

#endif /* INTEGRATION_CHAT_H */
