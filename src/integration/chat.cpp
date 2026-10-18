/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file chat.cpp Backend-admitted communication using the unchanged native wire. */

#include "../stdafx.h"
#include "chat.h"
#include "actions_internal.h"
#include "integration_bridge.h"
#include "../company_base.h"
#include "../console_type.h"
#include "../core/utf8.hpp"
#include "../network/network.h"
#include "../network/network_base.h"
#include "../network/network_func.h"
#include "../network/network_server.h"
#include "../string_func.h"
#include "../timer/timer_game_tick.h"
#include <charconv>

#include "../safeguards.h"

namespace Integration {
namespace {

constexpr size_t MAX_BODY_BYTES = 768;
constexpr size_t MAX_LABEL_BYTES = 80;
constexpr size_t MAX_PENDING_MESSAGES = 256;
constexpr size_t MAX_PENDING_BYTES = 256 * 1024;
constexpr size_t MAX_POLL_BYTES = 48 * 1024;

enum class NativeChatScope { World, Company, DirectCompany, Direct };

struct NativeChatSender {
	std::string public_key;
	CompanyID company = CompanyID::Invalid();
	std::string label;
};

struct NativeChatIngress {
	std::string message_id;
	NativeChatScope scope = NativeChatScope::World;
	std::string body;
	uint64_t tick = 0;
	NativeChatSender sender;
	CompanyID target_company = CompanyID::Invalid();
	std::string target_public_key;
	ClientID feedback_client = ClientID::Invalid;
	size_t encoded_bytes = 0;
};

struct NativeChatDelivery {
	std::string request_id;
	std::string message_id;
	std::string body;
	std::string label;
	std::string kind;
	std::optional<uint8_t> company;
	std::vector<std::string> recipient_keys;
};

/* Game-thread owned. No network handler or model work runs on another thread. */
std::string instance_id;
uint64_t ingress_counter = 0;
std::deque<NativeChatIngress> pending;
size_t pending_bytes = 0;

Json Error(std::string_view code, std::string_view message)
{
	return {{"error", {{"code", code}, {"message", message}}}};
}

void EnsureInstance()
{
	if (instance_id == InstanceId()) return;
	instance_id = InstanceId();
	ingress_counter = 0;
	pending.clear();
	pending_bytes = 0;
}

bool HasOnlyFields(const Json &object, std::initializer_list<std::string_view> fields)
{
	if (!object.is_object()) return false;
	for (const auto &[key, value] : object.items()) {
		if (std::ranges::find(fields, key) == fields.end()) return false;
	}
	return true;
}

bool ReadUnsigned(const Json &value, uint64_t &result)
{
	if (!value.is_number_integer()) return false;
	if (value.is_number_unsigned()) {
		result = value.get<uint64_t>();
	} else {
		auto number = value.get<int64_t>();
		if (number < 0) return false;
		result = static_cast<uint64_t>(number);
	}
	return true;
}

bool ValidKey(std::string_view key)
{
	return key.size() == NETWORK_SECRET_KEY_LENGTH - 1 && std::ranges::all_of(key, [](unsigned char c) {
		return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
	});
}

/** Prevent engine formatting codes, line/label injection and direction overrides. */
std::string DisplayText(std::string_view input, bool label)
{
	std::string valid = StrMakeValid(input, {StringValidationSetting::ReplaceWithQuestionMark, StringValidationSetting::ReplaceTabCrNlWithSpace});
	std::string result;
	for (char32_t c : Utf8View(valid)) {
		if (c == 0x7F || (c >= 0x80 && c < 0xA0) || IsTextDirectionChar(c) ||
				(c >= 0x2066 && c <= 0x2069) || c == 0x2028 || c == 0x2029 ||
				(label && (c == '[' || c == ']'))) {
			result += ' ';
		} else {
			auto [bytes, length] = EncodeUtf8(c);
			result.append(bytes, length);
		}
	}
	StrTrimInPlace(result);
	return result;
}

Json CompanyValue(CompanyID company)
{
	/* Preserve captured membership even if the company disappears before polling. */
	return company.base() < MAX_COMPANIES ? Json(company.base()) : Json(nullptr);
}

Json SenderValue(const NativeChatSender &sender)
{
	return {{"public_key", sender.public_key}, {"company_id", CompanyValue(sender.company)},
		{"label", sender.label}, {"kind", "human"}};
}

NativeChatSender AuthenticatedSender(const NetworkClientInfo &info)
{
	return {info.public_key, info.client_playas, DisplayText(info.client_name, true)};
}

Json IngressEnvelope(const NativeChatIngress &ingress)
{
	Json data = {{"message_id", ingress.message_id}, {"body", ingress.body}, {"tick", ingress.tick},
		{"sender", SenderValue(ingress.sender)}};
	switch (ingress.scope) {
		case NativeChatScope::World: data["scope"] = "world"; break;
		case NativeChatScope::Company: data["scope"] = "company"; break;
		case NativeChatScope::DirectCompany: data["scope"] = "direct_company"; break;
		case NativeChatScope::Direct: data["scope"] = "direct"; break;
	}
	if (ingress.scope == NativeChatScope::Company || ingress.scope == NativeChatScope::DirectCompany) data["target_company_id"] = ingress.target_company.base();
	if (ingress.scope == NativeChatScope::Direct) data["target_public_key"] = ingress.target_public_key;
	return {{"type", "chat.received"}, {"event_id", ingress.message_id}, {"data", std::move(data)}};
}

bool ActiveNativeSocket(const NetworkClientSocket &socket)
{
	/* Native delivery snapshots include only fully synchronized clients. */
	return socket.status == NetworkClientSocket::ClientStatus::Active && socket.GetInfo() != nullptr;
}

void PrivateFeedback(ClientID client)
{
	constexpr std::string_view text = "Chat unavailable or not permitted; try again later.";
	if (client == ClientID::Server && !_network_dedicated) {
		NetworkTextMessage(NetworkAction::ServerMessage, CC_DEFAULT, false, "", text);
		return;
	}
	if (auto *socket = NetworkClientSocket::GetByClientID(client); socket != nullptr && ActiveNativeSocket(*socket)) {
		socket->SendChat(NetworkAction::ServerMessage, ClientID::Server, false, text, 0);
	}
}

bool GeneratedIngressId(std::string_view id)
{
	std::string prefix = instance_id + ":chat:";
	if (!id.starts_with(prefix)) return false;
	id.remove_prefix(prefix.size());
	uint64_t number = 0;
	auto [end, error] = std::from_chars(id.data(), id.data() + id.size(), number);
	return !id.empty() && id.front() != '0' && error == std::errc{} && end == id.data() + id.size() && number > 0 && number <= ingress_counter;
}

} // namespace

bool InterceptNativeChat(NetworkAction action, NetworkChatDestinationType destination, int target,
		std::string_view body, ClientID sender_id, bool from_admin)
{
	if (!Enabled() || from_admin || (action != NetworkAction::ChatBroadcast && action != NetworkAction::ChatTeam && action != NetworkAction::ChatClient)) return false;
	/* Dedicated operator console announcements are not a native player identity. */
	if (sender_id == ClientID::Server && _network_dedicated) return false;
	EnsureInstance();
	const auto *sender = NetworkClientInfo::GetByClientID(sender_id);
	if (instance_id.empty() || sender == nullptr || !ValidKey(sender->public_key) || body.size() > MAX_BODY_BYTES || DisplayText(body, false).empty()) {
		PrivateFeedback(sender_id);
		return true;
	}

	NativeChatIngress ingress;
	ingress.sender = AuthenticatedSender(*sender);
	ingress.feedback_client = sender_id;
	ingress.body = body;
	ingress.tick = TimerGameTick::counter;
	bool valid = false;
	switch (destination) {
		case NetworkChatDestinationType::Broadcast:
			ingress.scope = NativeChatScope::World;
			valid = action == NetworkAction::ChatBroadcast;
			break;
		case NetworkChatDestinationType::Team:
			if (target >= 0 && target < MAX_COMPANIES && Company::IsValidID(CompanyID{static_cast<uint8_t>(target)})) {
				ingress.target_company = CompanyID{static_cast<uint8_t>(target)};
				ingress.scope = ingress.sender.company == ingress.target_company ? NativeChatScope::Company : NativeChatScope::DirectCompany;
				valid = action == NetworkAction::ChatTeam;
			}
			break;
		case NetworkChatDestinationType::Client: {
			const auto *recipient = NetworkClientInfo::GetByClientID(static_cast<ClientID>(target));
			if (recipient != nullptr && ValidKey(recipient->public_key)) {
				ingress.target_public_key = recipient->public_key;
				ingress.scope = NativeChatScope::Direct;
				valid = action == NetworkAction::ChatClient;
			}
			break;
		}
		default: break;
	}
	if (!valid || pending.size() >= MAX_PENDING_MESSAGES || ingress_counter == UINT64_MAX) {
		PrivateFeedback(sender_id);
		return true;
	}
	ingress.message_id = fmt::format("{}:chat:{}", instance_id, ingress_counter + 1);
	ingress.encoded_bytes = IngressEnvelope(ingress).dump().size();
	if (ingress.encoded_bytes > MAX_PENDING_BYTES - pending_bytes) {
		PrivateFeedback(sender_id);
		return true;
	}
	++ingress_counter;
	pending_bytes += ingress.encoded_bytes;
	pending.push_back(std::move(ingress));
	return true;
}

Json ChatPending(const Json &request)
{
	EnsureInstance();
	if (!HasOnlyFields(request, {"limit", "max_bytes"})) return Error("invalid_chat_poll", "Invalid pending-chat fields.");
	uint64_t limit = 32, max_bytes = MAX_POLL_BYTES;
	if ((request.contains("limit") && !ReadUnsigned(request["limit"], limit)) || limit == 0 || limit > 64 ||
			(request.contains("max_bytes") && !ReadUnsigned(request["max_bytes"], max_bytes)) || max_bytes < 2048 || max_bytes > MAX_POLL_BYTES) {
		return Error("invalid_chat_poll", "Pending chat requires limit 1..64 and max_bytes 2048..49152.");
	}
	Json result = {{"messages", Json::array()}, {"pending_count", pending.size()}, {"has_more", !pending.empty()}};
	size_t bytes = result.dump().size();
	if (!pending.empty() && pending.front().encoded_bytes + 1 > max_bytes - bytes) return Error("chat_poll_too_small", "Increase max_bytes to receive the oldest pending message.");
	for (const auto &ingress : pending) {
		if (result["messages"].size() >= limit || ingress.encoded_bytes + 1 > max_bytes - bytes) break;
		bytes += ingress.encoded_bytes + 1;
		result["messages"].push_back(IngressEnvelope(ingress));
	}
	result["has_more"] = result["messages"].size() < pending.size();
	return result;
}

Json ChatAcknowledge(const Json &request)
{
	EnsureInstance();
	if (!HasOnlyFields(request, {"ingress"}) || !request.contains("ingress")) return Error("invalid_chat_ack", "Invalid chat acknowledgement fields.");
	std::vector<std::pair<std::string, bool>> acknowledgements;
	if (request.contains("ingress")) {
		const auto &items = request["ingress"];
		if (!items.is_array() || items.size() > MAX_PENDING_MESSAGES) return Error("invalid_chat_ack", "Invalid ingress acknowledgement list.");
		for (const auto &item : items) {
			if (!HasOnlyFields(item, {"message_id", "accepted"}) || !item.contains("message_id") || !item["message_id"].is_string() ||
					!item.contains("accepted") || !item["accepted"].is_boolean()) return Error("invalid_chat_ack", "Invalid ingress acknowledgement.");
			auto id = item["message_id"].get<std::string>();
			if (!GeneratedIngressId(id)) return Error("unknown_chat_message", "Ingress message does not belong to this engine instance.");
			acknowledgements.emplace_back(std::move(id), item["accepted"].get<bool>());
		}
	}
	size_t removed = 0;
	for (const auto &[id, accepted] : acknowledgements) {
		auto found = std::ranges::find(pending, id, &NativeChatIngress::message_id);
		if (found == pending.end()) continue;
		if (!accepted) PrivateFeedback(found->feedback_client);
		pending_bytes -= found->encoded_bytes;
		pending.erase(found);
		++removed;
	}
	return {{"ok", true}, {"acknowledged", removed}, {"pending_count", pending.size()}};
}

Json DeliverChat(const Json &request)
{
	EnsureInstance();
	if (!_network_server || !Enabled()) return Error("world_not_running", "Native delivery requires an enabled world server.");
	if (!HasOnlyFields(request, {"delivery_request_id", "message_id", "body", "sender", "recipient_public_keys"})) return Error("invalid_chat_delivery", "Invalid delivery fields.");
	try {
		NativeChatDelivery delivery;
		delivery.request_id = request.at("delivery_request_id").get<std::string>();
		if (delivery.request_id.empty() || delivery.request_id.size() > 200) return Error("invalid_chat_delivery", "Invalid delivery request ID.");
		delivery.message_id = request.at("message_id").get<std::string>();
		delivery.body = request.at("body").get<std::string>();
		const auto &sender = request.at("sender");
		if (!HasOnlyFields(sender, {"label", "kind", "company_id"})) return Error("invalid_chat_delivery", "Invalid sender presentation fields.");
		delivery.label = sender.at("label").get<std::string>();
		delivery.kind = sender.at("kind").get<std::string>();
		if (!sender.at("company_id").is_null()) {
			uint64_t company;
			if (!ReadUnsigned(sender["company_id"], company) || company >= MAX_COMPANIES) return Error("invalid_chat_delivery", "Invalid sender company label.");
			delivery.company = static_cast<uint8_t>(company);
		}
		if (delivery.message_id.empty() || delivery.message_id.size() > 200 || delivery.body.size() > MAX_BODY_BYTES ||
				delivery.label.size() > MAX_LABEL_BYTES || (delivery.kind != "human" && delivery.kind != "agent")) return Error("invalid_chat_delivery", "Invalid or oversized chat presentation.");
		delivery.body = DisplayText(delivery.body, false);
		delivery.label = DisplayText(delivery.label, true);
		if (delivery.body.empty() || delivery.label.empty()) return Error("invalid_chat_delivery", "Chat requires visible sender and body text.");
		const auto &recipients = request.at("recipient_public_keys");
		if (!recipients.is_array() || recipients.size() > MAX_CLIENTS + 1) return Error("invalid_chat_delivery", "Invalid authorized native recipient list.");
		for (const auto &recipient : recipients) {
			if (!recipient.is_string()) return Error("invalid_chat_delivery", "Native recipient must be an authenticated public key.");
			auto key = recipient.get<std::string>();
			if (!ValidKey(key)) return Error("invalid_chat_delivery", "Invalid native recipient public key.");
			strtolower(key);
			delivery.recipient_keys.push_back(std::move(key));
		}
		std::ranges::sort(delivery.recipient_keys);
		delivery.recipient_keys.erase(std::unique(delivery.recipient_keys.begin(), delivery.recipient_keys.end()), delivery.recipient_keys.end());
		std::string message = fmt::format("[{}: {}{}] {}", delivery.kind == "agent" ? "Agent" : "Human", delivery.label,
				delivery.company ? fmt::format(" / C{}", *delivery.company + 1) : std::string{}, delivery.body);
		if (message.size() >= NETWORK_CHAT_LENGTH) return Error("invalid_chat_delivery", "Formatted chat exceeds the native wire limit.");
		auto selected = [&delivery](const NetworkClientInfo &info) {
			return std::ranges::any_of(delivery.recipient_keys, [&info](const auto &key) { return StrEqualsIgnoreCase(info.public_key, key); });
		};
		size_t count = 0;
		for (auto *socket : NetworkClientSocket::Iterate()) {
			if (!ActiveNativeSocket(*socket) || !selected(*socket->GetInfo())) continue;
			/* Known native identity; no central routing, event echo or AdminPort copy. */
			socket->SendChat(NetworkAction::ServerMessage, ClientID::Server, false, message, 0);
			++count;
		}
		if (!_network_dedicated) {
			const auto *local = NetworkClientInfo::GetByClientID(ClientID::Server);
			if (local != nullptr && selected(*local)) {
				NetworkTextMessage(NetworkAction::ServerMessage, CC_DEFAULT, false, "", message);
				++count;
			}
		}
		Json result = {{"ok", true}, {"delivery_request_id", delivery.request_id}, {"message_id", delivery.message_id},
			{"native_recipients", count}, {"tick", TimerGameTick::counter}};
		return result;
	} catch (const Json::exception &) {
		return Error("invalid_chat_delivery", "Missing or invalid native delivery fields.");
	}
}

Json NativeRoster()
{
	Json result = Json::array();
	for (const auto *socket : NetworkClientSocket::Iterate()) {
		if (ActiveNativeSocket(*socket) && ValidKey(socket->GetInfo()->public_key)) {
			Json entry = SenderValue(AuthenticatedSender(*socket->GetInfo()));
			entry["native_handle"] = Detail::NativeParticipantHandle(to_underlying(socket->GetInfo()->client_id));
			result.push_back(std::move(entry));
		}
	}
	if (!_network_dedicated) {
		const auto *local = NetworkClientInfo::GetByClientID(ClientID::Server);
		if (local != nullptr && ValidKey(local->public_key)) result.push_back(SenderValue(AuthenticatedSender(*local)));
	}
	return result;
}

} // namespace Integration
