/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file actions_access.cpp Own-company access policy and ordinary loan commands. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "renderer.h"
#include "renderer_input.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../company_cmd.h"
#include "../misc_cmd.h"
#include "../network/network.h"
#include "../network/network_base.h"
#include "../network/network_server.h"

#include "../safeguards.h"

namespace Integration::Detail {
namespace {

const NetworkClientInfo *ActiveParticipant(ClientID id)
{
	if (!_network_server) throw InvalidParameters("Participant handles require the authoritative server");
	const NetworkClientInfo *info = NetworkClientInfo::GetByClientID(id);
	const NetworkClientSocket *socket = NetworkClientSocket::GetByClientID(id);
	if (info == nullptr || socket == nullptr || socket->status != NetworkClientSocket::ClientStatus::Active || socket->GetInfo() != info) throw InvalidParameters("Native participant is no longer connected");
	const std::string &key = info->public_key;
	if (key.size() != 64 || !std::ranges::all_of(key, [](unsigned char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'); })) throw InvalidParameters("Native participant key is invalid");
	return info;
}

Json ParticipantHandle(const NetworkClientInfo *info)
{
	/* ClientID identifies the connection. The pool index is a separate identifier. */
	return {{"id", to_underlying(info->client_id)}, {"generation", info->GetInstanceGeneration()}, {"engine_instance_id", InstanceId()}};
}

const NetworkClientInfo *ReadParticipant(const Json &parameters)
{
	const Json &handle = parameters.at("participant");
	if (!handle.is_object() || handle.size() != 3 || !handle.contains("id") || !handle.contains("generation") || !handle.contains("engine_instance_id")) throw InvalidParameters("Participant requires a complete native connection handle");
	if (!handle.at("engine_instance_id").is_string() || handle.at("engine_instance_id").get_ref<const std::string &>() != InstanceId()) throw InvalidParameters("Participant belongs to another engine instance");
	Parameters p(handle);
	ClientID id = static_cast<ClientID>(static_cast<uint32_t>(p.Int("id", 0, UINT32_MAX)));
	const NetworkClientInfo *info = ActiveParticipant(id);
	const Json &generation = handle.at("generation");
	if (!generation.is_number_integer() || (!generation.is_number_unsigned() && generation.get<int64_t>() <= 0) || generation.get<uint64_t>() != info->GetInstanceGeneration()) throw InvalidParameters("Native participant connection is stale");
	return info;
}

CompanyAllowListCtrlAction JoinPolicy(const Parameters &p)
{
	auto policy = p.Text("policy");
	if (policy == "any") return CompanyAllowListCtrlAction::AllowAny;
	if (policy == "listed") return CompanyAllowListCtrlAction::AllowListed;
	throw InvalidParameters("policy must be any or listed");
}

LoanCommand LoanMode(const Json &parameters)
{
	Parameters p(parameters);
	auto mode = p.Text("mode", "amount");
	if (mode == "amount") {
		p.Int("amount", 1, INT64_MAX);
		return LoanCommand::Amount;
	}
	if (parameters.contains("amount")) throw InvalidParameters("amount is only valid in amount loan mode");
	if (mode == "interval") return LoanCommand::Interval;
	if (mode == "max") return LoanCommand::Max;
	throw InvalidParameters("mode must be amount, interval or max");
}

/** Guard only unsafe residual predicates; native funds are evaluated at execution. */
void CheckLoanSafety(bool increase, LoanCommand mode)
{
	const Company *company = Company::Get(_current_company);
	if (!increase && mode == LoanCommand::Max && !(company->current_loan == 0 || company->current_loan >= LOAN_INTERVAL)) throw InvalidParameters("Use interval mode to repay a residual loan below loan_interval");
	Money remaining = company->GetMaxLoan() - company->current_loan;
	if (increase && mode == LoanCommand::Interval && remaining > 0 && remaining < LOAN_INTERVAL) throw InvalidParameters("Use max mode when remaining loan headroom is below loan_interval");
}

template <Commands Tcommand>
typename CommandTraits<Tcommand>::Args Decode(const CommandDataBuffer &data)
{
	using Args = typename CommandTraits<Tcommand>::Args;
	if (data.size() > 512) throw InvalidParameters("Native access command data is oversized");
	auto args = EndianBufferReader::ToValue<Args>(data);
	if (EndianBufferWriter<CommandDataBuffer>::FromValue(args) != data) throw InvalidParameters("Native access command data is malformed or noncanonical");
	return args;
}

/** Compare only fields which affect permission, never expose this private proof. */
Json ParticipantProof(const NetworkClientInfo *info, bool generations)
{
	Json result = {{"client_id", to_underlying(info->client_id)}, {"public_key", info->public_key}};
	if (generations) result["generation"] = info->GetInstanceGeneration();
	return result;
}

Json ParticipantsByKey(const std::string &key)
{
	if (!_network_server && !RendererIsActive()) throw InvalidParameters("Native participant authorization requires a native participant context");
	if (key.size() != 64 || !std::ranges::all_of(key, [](unsigned char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'); })) throw InvalidParameters("Native participant key is invalid");
	std::vector<uint32_t> connections;
	for (const NetworkClientInfo *info : NetworkClientInfo::Iterate()) {
		if (info->client_id == ClientID::Invalid || info->client_id == ClientID::Server || info->public_key != key) continue;
		if (_network_server) {
			const NetworkClientSocket *socket = NetworkClientSocket::GetByClientID(info->client_id);
			if (socket == nullptr || socket->status != NetworkClientSocket::ClientStatus::Active) continue;
			ActiveParticipant(info->client_id);
		}
		connections.push_back(to_underlying(info->client_id));
	}
	if (connections.empty()) throw InvalidParameters("Native participant is no longer connected");
	std::ranges::sort(connections);
	/* Permission is per key; one human may have several live native connections. */
	return {{"public_key", key}, {"client_ids", std::move(connections)}};
}

} // namespace

/** Private roster projection; backend removes worker entries before public exposure. */
Json NativeParticipantHandle(uint32_t client_id)
{
	return ParticipantHandle(ActiveParticipant(static_cast<ClientID>(client_id)));
}

std::optional<ActionResult> DispatchAccessAction(const std::string &action, const Json &parameters, ActionInvocation &invocation)
{
	Parameters p(parameters);
	if (action == "company.join_policy.set") return invocation.Post<Commands::CompanyAllowListControl>(JoinPolicy(p), "");
	if (action == "company.participant.authorize") {
		const NetworkClientInfo *target = ReadParticipant(parameters);
		return invocation.Post<Commands::CompanyAllowListControl>(p.Bool("allowed") ? CompanyAllowListCtrlAction::AddKey : CompanyAllowListCtrlAction::RemoveKey, target->public_key);
	}
	if (action == "company.loan.increase" || action == "company.loan.decrease") {
		LoanCommand mode = LoanMode(parameters);
		bool increase = action == "company.loan.increase";
		CheckLoanSafety(increase, mode);
		Money amount = mode == LoanCommand::Amount ? p.Int("amount", 1, INT64_MAX) : 0;
		return increase ? invocation.Post<Commands::IncreaseLoan>(mode, amount) : invocation.Post<Commands::DecreaseLoan>(mode, amount);
	}
	return std::nullopt;
}

std::optional<Json> CaptureAccessTargets(const std::string &action, const Json &parameters)
{
	if (action == "company.join_policy.set") {
		JoinPolicy(Parameters(parameters));
		return Json::array();
	}
	if (action == "company.participant.authorize") return Json::array({ParticipantProof(ReadParticipant(parameters), true)});
	if (action == "company.loan.increase" || action == "company.loan.decrease") {
		CheckLoanSafety(action == "company.loan.increase", LoanMode(parameters));
		/* Recalculation repeats the predicate without pinning loan or cash amounts. */
		return Json::array();
	}
	return std::nullopt;
}

std::optional<Json> QueryAccessState(const std::string &query, const Json &parameters)
{
	if (query != "company.access") return std::nullopt;
	const Company *company = Company::Get(_current_company);
	Json result = SnapshotBase();
	result["join_policy"] = company->allow_any ? "any" : "listed";
	if (parameters.contains("participant")) {
		const NetworkClientInfo *participant = ReadParticipant(parameters);
		bool listed = company->allow_list.Contains(participant->public_key);
		result["participant"] = parameters.at("participant");
		result["listed"] = listed;
		result["permitted"] = company->allow_any || listed;
	}
	return result;
}

std::optional<Json> AccessIntentState(Commands command, const CommandDataBuffer &data)
{
	if (command == Commands::IncreaseLoan || command == Commands::DecreaseLoan) {
		auto args = command == Commands::IncreaseLoan ? Decode<Commands::IncreaseLoan>(data) : Decode<Commands::DecreaseLoan>(data);
		auto [mode, amount] = args;
		if (mode != LoanCommand::Amount && mode != LoanCommand::Interval && mode != LoanCommand::Max) throw InvalidParameters("Invalid native loan mode");
		CheckLoanSafety(command == Commands::IncreaseLoan, mode);
		return Json::object();
	}
	if (command != Commands::CompanyAllowListControl) return std::nullopt;
	auto [action, key] = Decode<Commands::CompanyAllowListControl>(data);
	if (RendererIsActive() && !RendererAccessCommandMatches(action, key)) throw InvalidParameters("Visual company access has no matching PNG control lease");
	if (action == CompanyAllowListCtrlAction::AllowAny || action == CompanyAllowListCtrlAction::AllowListed) {
		if (!key.empty()) throw InvalidParameters("Join policy takes no key");
		return Json::object();
	}
	if (action != CompanyAllowListCtrlAction::AddKey && action != CompanyAllowListCtrlAction::RemoveKey) throw InvalidParameters("Invalid native company access action");
	/* Worker row provenance is local; only native connection identities cross replicas. */
	/* Local queued Posts do not pass the ordinary native-client ingress check. */
	return ParticipantsByKey(key);
}

} // namespace Integration::Detail
