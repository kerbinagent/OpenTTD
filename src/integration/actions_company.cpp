/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file actions_company.cpp Normal company appearance commands and bounded company reports. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../company_cmd.h"
#include "../company_manager_face.h"
#include "../economy_cmd.h"
#include "../economy_func.h"
#include "../engine_base.h"
#include "../engine_cmd.h"
#include "../strings_func.h"
#include "../timer/timer_game_calendar.h"
#include "../timer/timer_game_economy.h"

#include <chrono>
#include <optional>

#include "table/strings.h"

#include "../safeguards.h"

namespace Integration::Detail {
namespace {

static constexpr std::array<std::string_view, to_underlying(LiveryScheme::End)> LIVERY_NAMES = {
	"default", "steam", "diesel", "electric", "monorail", "maglev", "dmu", "emu",
	"passenger_wagon_steam", "passenger_wagon_diesel", "passenger_wagon_electric", "passenger_wagon_monorail", "passenger_wagon_maglev",
	"freight_wagon", "bus", "truck", "passenger_ship", "freight_ship", "helicopter", "small_plane", "large_plane", "passenger_tram", "freight_tram",
};
static constexpr std::array<std::string_view, to_underlying(Colours::End)> COLOUR_NAMES = {
	"dark_blue", "pale_green", "pink", "yellow", "red", "light_blue", "green", "dark_green", "blue", "cream", "mauve", "purple", "orange", "brown", "grey", "white",
};

LiveryScheme ReadLivery(const Parameters &parameters)
{
	auto name = parameters.Text("scheme");
	auto it = std::ranges::find(LIVERY_NAMES, name);
	if (it == LIVERY_NAMES.end()) throw InvalidParameters("Unknown company livery scheme");
	return static_cast<LiveryScheme>(it - LIVERY_NAMES.begin());
}

bool ReadPrimary(const Parameters &parameters)
{
	auto name = parameters.Text("channel");
	if (name == "primary") return true;
	if (name == "secondary") return false;
	throw InvalidParameters("Livery channel must be primary or secondary");
}

Colours ReadColour(const Parameters &parameters)
{
	auto name = parameters.Text("colour");
	if (name == "inherit") return Colours::Invalid;
	auto it = std::ranges::find(COLOUR_NAMES, name);
	if (it == COLOUR_NAMES.end()) throw InvalidParameters("Unknown company livery colour");
	return static_cast<Colours>(it - COLOUR_NAMES.begin());
}

const Company *ReadCompanyHandle(const Json &parameters)
{
	const auto &handle = parameters.at("company_id");
	if (!handle.is_object() || handle.size() != 3 || !handle.contains("id") || !handle.contains("generation") || !handle.contains("engine_instance_id")) throw InvalidParameters("Company target requires a complete handle");
	if (!handle.at("engine_instance_id").is_string() || handle.at("engine_instance_id").get_ref<const std::string &>() != InstanceId()) throw InvalidParameters("Company target belongs to another engine instance");
	Parameters fields(handle);
	auto id = CompanyID(static_cast<uint8_t>(fields.Int("id", 0, MAX_COMPANIES - 1)));
	const Company *company = Company::GetIfValid(id);
	const auto &generation = handle.at("generation");
	if (company == nullptr || !generation.is_number_integer() || (generation.is_number_integer() && !generation.is_number_unsigned() && generation.get<int64_t>() < 0) || generation.get<uint64_t>() != company->GetInstanceGeneration()) throw InvalidParameters("Company target is invalid or stale");
	return company;
}

FaceVars CheckedFaceVars(uint style)
{
	if (style >= GetNumCompanyManagerFaceStyles()) throw InvalidParameters("Manager face style is unavailable");
	auto vars = GetCompanyManagerFaceVars(style);
	/* Native activation masks use one uint64_t. Keep the shift below its width. */
	if (vars.size() >= 64) throw InvalidParameters("Manager face component catalog exceeds its bound");
	return vars;
}

CompanyManagerFace ChangedFace(const std::string &action, const Parameters &parameters)
{
	CompanyManagerFace face = Company::Get(_current_company)->face;
	if (action == "company.manager_face.style.set") {
		uint style = static_cast<uint>(parameters.Int("style_id", 0, UINT32_MAX));
		auto vars = CheckedFaceVars(style);
		SetCompanyManagerFaceStyle(face, style);
		ScaleAllCompanyManagerFaceBits(face, vars);
	} else {
		uint expected = static_cast<uint>(parameters.Int("expected_style_id", 0, UINT32_MAX));
		if (face.style != expected) throw InvalidParameters("Manager face style changed; refresh its component catalog");
		auto vars = CheckedFaceVars(face.style);
		uint component = static_cast<uint>(parameters.Int("component_id", 0, 63));
		if (component >= vars.size() || vars[component].name == STR_NULL || !HasBit(GetActiveFaceVars(face, vars), component)) throw InvalidParameters("Manager face component is not currently editable");
		const FaceVar &variable = vars[component];
		if (variable.valid_values == 0) throw InvalidParameters("Manager face component has no valid values");
		variable.SetBits(face, static_cast<uint>(parameters.Int("value", 0, variable.valid_values - 1)));
		/* Toggling a component can enable other fields, just as in the native dialog. */
		ScaleAllCompanyManagerFaceBits(face, vars);
	}
	return face;
}

Json FaceProof()
{
	const CompanyManagerFace &face = Company::Get(_current_company)->face;
	return {{"style_id", face.style}, {"bits", face.bits}};
}

Json LiveryProof(LiveryScheme scheme, bool primary, bool inherit)
{
	const Company *company = Company::Get(_current_company);
	const Livery &livery = company->livery[scheme];
	Json state = {{"scheme", to_underlying(scheme)}, {"primary", primary},
		{"colour", to_underlying(primary ? livery.colour1 : livery.colour2)},
		{"explicit", livery.in_use.Test(primary ? Livery::Flag::Primary : Livery::Flag::Secondary)}};
	if (inherit) state["default_colour"] = to_underlying(primary ? company->livery[LiveryScheme::Default].colour1 : company->livery[LiveryScheme::Default].colour2);
	return state;
}

Json AcquisitionProof(const Company *company)
{
	/* Per-process handle fences do not belong in renderer/authority state comparisons. */
	return {{"company_id", company->index.base()}, {"offered", company->bankrupt_asked.Test(_current_company)}, {"offer_price", company->bankrupt_value.base()}};
}

const Engine *ReadPreviewEngine(const Parameters &p)
{
	const Engine *engine = Engine::GetIfValid(EngineID(static_cast<uint16_t>(p.Int("engine_id", 0, UINT16_MAX))));
	if (engine == nullptr) throw InvalidParameters("Engine preview target is unavailable");
	return engine;
}

Json PreviewProof(const Engine *engine)
{
	return {{"engine_id", engine->index.base()}, {"exclusive_preview", engine->flags.Test(EngineFlag::ExclusivePreview)},
		{"preview_company", engine->preview_company.base()}, {"intro_date", engine->intro_date.base()}, {"preview_asked", engine->preview_asked.base()}};
}

const char *FaceVariableType(FaceVarType type)
{
	switch (type) {
		case FaceVarType::Sprite: return "sprite";
		case FaceVarType::Palette: return "palette";
		case FaceVarType::Toggle: return "toggle";
	}
	throw InvalidParameters("Unknown manager face component type");
}

Json FaceComponents(uint style, const CompanyManagerFace *face = nullptr)
{
	auto vars = CheckedFaceVars(style);
	Json components = Json::array();
	uint64_t active = face == nullptr ? 0 : GetActiveFaceVars(*face, vars);
	for (size_t index = 0; index < vars.size(); ++index) {
		const FaceVar &variable = vars[index];
		if (variable.name == STR_NULL) continue;
		Json item = {{"component_id", index}, {"name", GetString(variable.name)}, {"type", FaceVariableType(variable.type)}, {"value_count", variable.valid_values}};
		if (face != nullptr) {
			item["active"] = HasBit(active, index);
			if (HasBit(active, index)) item["value"] = variable.GetBits(*face);
		}
		components.push_back(std::move(item));
	}
	return components;
}

Json CompanyAppearance()
{
	const Company *company = Company::Get(_current_company);
	const FaceSpec *style = GetCompanyManagerFaceSpec(company->face.style);
	if (style == nullptr) throw InvalidParameters("Company manager face style is unavailable");
	Json liveries = Json::array();
	for (LiveryScheme scheme : EnumRange(LiveryScheme::Begin, LiveryScheme::End)) {
		const Livery &livery = company->livery[scheme];
		liveries.push_back({{"scheme", LIVERY_NAMES[to_underlying(scheme)]},
			{"primary", COLOUR_NAMES[to_underlying(livery.colour1)]}, {"secondary", COLOUR_NAMES[to_underlying(livery.colour2)]},
			{"primary_inherited", scheme != LiveryScheme::Default && !livery.in_use.Test(Livery::Flag::Primary)},
			{"secondary_inherited", scheme != LiveryScheme::Default && !livery.in_use.Test(Livery::Flag::Secondary)}});
	}
	return {{"president_name", GetString(STR_PRESIDENT_NAME, company->index)},
		{"manager_face", {{"style_id", company->face.style}, {"style_label", style->label}, {"components", FaceComponents(company->face.style, &company->face)}}},
		{"liveries", std::move(liveries)}};
}

Json FaceCatalog(const Parameters &p)
{
	uint style = static_cast<uint>(p.Int("style_id", 0, UINT32_MAX, Company::Get(_current_company)->face.style));
	CheckedFaceVars(style);
	uint64_t next = static_cast<uint64_t>(p.Int("after_id", -1, UINT32_MAX, -1) + 1);
	size_t limit = static_cast<size_t>(p.Int("limit", 1, 16, 16));
	Json items = Json::array();
	while (next < GetNumCompanyManagerFaceStyles() && items.size() < limit) {
		const FaceSpec *spec = GetCompanyManagerFaceSpec(static_cast<uint>(next));
		items.push_back({{"style_id", next}, {"label", spec->label}});
		++next;
	}
	return {{"items", std::move(items)}, {"next_after_id", next < GetNumCompanyManagerFaceStyles() ? Json(next - 1) : Json(nullptr)},
		{"selected_style", {{"style_id", style}, {"label", GetCompanyManagerFaceSpec(style)->label}, {"components", FaceComponents(style)}}}};
}

Json Dates()
{
	auto calendar = TimerGameCalendar::ConvertDateToYMD(TimerGameCalendar::date);
	auto economy = TimerGameEconomy::ConvertDateToYMD(TimerGameEconomy::date);
	return {{"calendar_date", {{"date", TimerGameCalendar::date.base()}, {"year", calendar.year.base()}, {"month", calendar.month + 1}, {"day", calendar.day}}},
		{"economy_date", {{"date", TimerGameEconomy::date.base()}, {"year", economy.year.base()}, {"month", economy.month + 1}, {"day", economy.day}, {"quarter", economy.month / 3 + 1}, {"wallclock", TimerGameEconomy::UsingWallclockUnits()}}}};
}

Json EconomyQuarter(const CompanyEconomyEntry &entry, int64_t quarter)
{
	return {{"period", quarter}, {"year", quarter / 4}, {"quarter", quarter % 4 + 1},
		{"income", entry.income.base()}, {"expenses", entry.expenses.base()}, {"cargo_delivered", entry.delivered_cargo.GetSum<uint64_t>()}};
}

const Company *PublicReportCompany(const Parameters &p)
{
	auto id = CompanyID(static_cast<uint8_t>(p.Int("company_id", 0, MAX_COMPANIES - 1, _current_company.base())));
	const Company *company = Company::GetIfValid(id);
	if (company == nullptr) throw InvalidParameters("Company report target is unavailable");
	return company;
}

Json CompanyReport(const Parameters &p)
{
	const Company *company = PublicReportCompany(p);
	Json result = Dates();
	result["company"] = Handle(company);
	result["name"] = GetString(STR_COMPANY_NAME, company->index);
	result["president_name"] = GetString(STR_PRESIDENT_NAME, company->index);
	result["balance"] = company->money.base();
	result["loan"] = company->current_loan.base();
	result["maximum_loan"] = company->GetMaxLoan().base();
	result["inaugurated_year"] = company->inaugurated_year.base();
	result["inaugurated_calendar_year"] = company->inaugurated_year_calendar.base();
	result["vehicle_counts"] = Json::object();
	for (VehicleType type : EnumRange(VehicleType::CompanyEnd)) result["vehicle_counts"][VehicleTypeName(type)] = company->group_all[type].num_vehicle;
	result["infrastructure"] = {{"rail", company->infrastructure.GetRailTotal()}, {"road", company->infrastructure.GetRoadTotal()}, {"tram", company->infrastructure.GetTramTotal()},
		{"signals", company->infrastructure.signal}, {"water", company->infrastructure.water}, {"station_tiles", company->infrastructure.station}, {"airports", company->infrastructure.airport}};
	int64_t current_quarter = static_cast<int64_t>(TimerGameEconomy::year.base()) * 4 + TimerGameEconomy::month / 3;
	result["current_quarter"] = EconomyQuarter(company->cur_economy, current_quarter);
	result["quarter_history"] = Json::array();
	for (size_t index = 0; index < std::min<size_t>(company->num_valid_stat_ent, company->old_economy.size()); ++index) {
		result["quarter_history"].push_back(EconomyQuarter(company->old_economy[index], current_quarter - static_cast<int64_t>(index) - 1));
	}
	static constexpr std::array<std::string_view, to_underlying(ExpensesType::End)> EXPENSE_NAMES = {
		"construction", "new_vehicles", "train_running", "road_running", "aircraft_running", "ship_running", "property",
		"train_revenue", "road_revenue", "aircraft_revenue", "ship_revenue", "loan_interest", "other",
	};
	result["yearly_expenses"] = Json::array();
	for (size_t index = 0; index < company->yearly_expenses.size(); ++index) {
		Json categories = Json::object();
		for (ExpensesType type : EnumRange(ExpensesType::Begin, ExpensesType::End)) categories[EXPENSE_NAMES[to_underlying(type)]] = company->yearly_expenses[index][type].base();
		result["yearly_expenses"].push_back({{"year", TimerGameEconomy::year.base() - static_cast<int64_t>(index)}, {"categories", std::move(categories)}});
	}
	/* This offer is private to the company currently asked to buy it. */
	if (company->bankrupt_asked.Test(_current_company)) result["acquisition_offer"] = {{"price", company->bankrupt_value.base()}};
	return result;
}

Json CompanyValuation(const Parameters &p)
{
	const Company *company = PublicReportCompany(p);
	/* One fresh native pool scan per monotonic second across all actors and targets.
	 * Reserve before the scan so its own duration cannot open an immediate retry. */
	static std::optional<std::chrono::steady_clock::time_point> last_start;
	const auto now = std::chrono::steady_clock::now();
	if (last_start.has_value() && now - *last_start < std::chrono::seconds(1)) throw InvalidParameters("Company valuation is rate limited; retry later");
	last_start = now;
	return {{"company", Handle(company)}, {"company_value", CalculateCompanyValue(company).base()}};
}

Json PreviewOffers(const Parameters &p)
{
	uint64_t next = static_cast<uint64_t>(p.Int("after_id", -1, UINT32_MAX, -1) + 1);
	size_t limit = static_cast<size_t>(p.Int("limit", 1, 64, 64));
	uint scanned = 0;
	Json items = Json::array();
	while (next < Engine::GetPoolSize() && scanned < 4096 && items.size() < limit) {
		const Engine *engine = Engine::GetIfValid(next);
		if (engine != nullptr && engine->flags.Test(EngineFlag::ExclusivePreview) && engine->preview_company == _current_company) {
			uint16_t mail_capacity = 0;
			uint capacity = engine->GetDisplayDefaultCapacity(&mail_capacity);
			items.push_back({{"engine_id", engine->index.base()}, {"name", GetString(STR_ENGINE_NAME, engine->index)}, {"vehicle_type", VehicleTypeName(engine->type)},
				{"price", engine->GetCost().base()}, {"running_cost", engine->GetRunningCost().base()}, {"maximum_speed", engine->GetDisplayMaxSpeed()},
				{"default_cargo_id", to_underlying(engine->GetDefaultCargoType())}, {"capacity", capacity}, {"mail_capacity", mail_capacity}});
		}
		++next;
		++scanned;
	}
	return {{"items", std::move(items)}, {"next_after_id", next < Engine::GetPoolSize() ? Json(next - 1) : Json(nullptr)}};
}

} // namespace

std::span<const std::string_view> CompanyLiveryNames() { return LIVERY_NAMES; }
std::span<const std::string_view> CompanyColourNames() { return COLOUR_NAMES; }

std::optional<ActionResult> DispatchCompanyAction(const std::string &action, const Json &parameters, ActionInvocation &invocation)
{
	Parameters p(parameters);
	if (action == "company.president.rename") return invocation.Post<Commands::RenamePresident>(p.Text("name"));
	if (action == "company.livery.set") return invocation.Post<Commands::SetCompanyColour>(ReadLivery(p), ReadPrimary(p), ReadColour(p));
	if (action == "company.buy") return invocation.Post<Commands::BuyCompany>(ReadCompanyHandle(parameters)->index, false);
	if (action == "engine.preview.accept") return invocation.Post<Commands::WantEnginePreview>(ReadPreviewEngine(p)->index);
	if (action == "company.manager_face.style.set" || action == "company.manager_face.component.set") {
		auto face = ChangedFace(action, p);
		return invocation.Post<Commands::SetCompanyManagerFace>(face.style, face.bits);
	}
	return std::nullopt;
}

std::optional<Json> CaptureCompanyTargets(const std::string &action, const Json &parameters)
{
	Parameters p(parameters);
	if (action == "company.president.rename") return Json::array();
	if (action == "company.livery.set") return Json::array({{{"kind", "company_livery"}, {"state", LiveryProof(ReadLivery(p), ReadPrimary(p), ReadColour(p) == Colours::Invalid)}}});
	if (action == "company.buy") {
		const Company *company = ReadCompanyHandle(parameters);
		return Json::array({{{"kind", "company_acquisition"}, {"target", Handle(company)}, {"state", AcquisitionProof(company)}}});
	}
	if (action == "engine.preview.accept") return Json::array({{{"kind", "engine_preview"}, {"state", PreviewProof(ReadPreviewEngine(p))}}});
	if (action == "company.manager_face.style.set" || action == "company.manager_face.component.set") {
		ChangedFace(action, p);
		return Json::array({{{"kind", "company_face"}, {"state", FaceProof()}}});
	}
	return std::nullopt;
}

std::optional<Json> QueryCompanyState(const std::string &query, const Json &parameters)
{
	Parameters p(parameters);
	Json result;
	if (query == "company.appearance") result = CompanyAppearance();
	else if (query == "catalog.manager_faces") result = FaceCatalog(p);
	else if (query == "company.report") result = CompanyReport(p);
	else if (query == "company.valuation") result = CompanyValuation(p);
	else if (query == "engine.preview_offers") result = PreviewOffers(p);
	else return std::nullopt;
	result.update(SnapshotBase());
	if (result.dump().size() > 48000) throw InvalidParameters("Company report exceeds the observation byte budget");
	return result;
}

std::optional<Json> CompanyIntentState(Commands command, const CommandDataBuffer &data)
{
	if (command == Commands::SetCompanyManagerFace) return Json{{"company_face", FaceProof()}};
	if (command == Commands::SetCompanyColour) {
		auto [scheme, primary, colour] = EndianBufferReader::ToValue<CommandTraits<Commands::SetCompanyColour>::Args>(data);
		if (scheme >= LiveryScheme::End) throw InvalidParameters("Invalid native livery scheme");
		return Json{{"company_livery", LiveryProof(scheme, primary, colour == Colours::Invalid)}};
	}
	if (command == Commands::BuyCompany) {
		auto [id, hostile] = EndianBufferReader::ToValue<CommandTraits<Commands::BuyCompany>::Args>(data);
		if (hostile) throw InvalidParameters("Hostile takeover is outside the multiplayer action surface");
		const Company *company = Company::GetIfValid(id);
		if (company == nullptr) throw InvalidParameters("Native acquisition target is unavailable");
		return Json{{"company_acquisition", AcquisitionProof(company)}};
	}
	if (command == Commands::WantEnginePreview) {
		auto [id] = EndianBufferReader::ToValue<CommandTraits<Commands::WantEnginePreview>::Args>(data);
		const Engine *engine = Engine::GetIfValid(id);
		if (engine == nullptr) throw InvalidParameters("Native engine preview target is unavailable");
		return Json{{"engine_preview", PreviewProof(engine)}};
	}
	return std::nullopt;
}

} // namespace Integration::Detail
