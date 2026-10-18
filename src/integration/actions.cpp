/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file actions.cpp Typed public gameplay actions and bounded text-mode snapshots. */

#include "../stdafx.h"
#include "actions.h"
#include "actions_internal.h"
#include "actions_limits.h"
#include "actions_vehicle_scope.h"
#include "actions_limits.h"
#include "actions_spatial.h"
#include "actions_story.h"
#include "../3rdparty/monocypher/monocypher.h"
#include "actions_story.h"
#include "integration_bridge.h"
#include "../aircraft.h"
#include "../airport.h"
#include "../articulated_vehicles.h"
#include "../autoreplace_cmd.h"
#include "../autoreplace_func.h"
#include "../bridge.h"
#include "../cargotype.h"
#include "../command_func.h"
#include "../core/random_func.hpp"
#include "../company_base.h"
#include "../company_cmd.h"
#include "../company_func.h"
#include "../depot_base.h"
#include "../depot_map.h"
#include "../engine_base.h"
#include "../engine_cmd.h"
#include "../engine_func.h"
#include "../industry.h"
#include "../industry_map.h"
#include "../industry_cmd.h"
#include "../group.h"
#include "../group_cmd.h"
#include "../goal_cmd.h"
#include "../house.h"
#include "../landscape.h"
#include "../landscape_cmd.h"
#include "../misc_cmd.h"
#include "../newgrf_airport.h"
#include "../object_base.h"
#include "../object_cmd.h"
#include "../order_cmd.h"
#include "../rail.h"
#include "../rail_cmd.h"
#include "../rail_map.h"
#include "../road.h"
#include "../road_cmd.h"
#include "../road_map.h"
#include "../roadveh_cmd.h"
#include "../station_base.h"
#include "../station_cmd.h"
#include "../station_map.h"
#include "../settings_cmd.h"
#include "../signs_base.h"
#include "../signs_cmd.h"
#include "../signs_func.h"
#include "../strings_func.h"
#include "../string_func.h"
#include "../story_cmd.h"
#include "../terraform_cmd.h"
#include "../tilearea_type.h"
#include "../timer/timer_game_tick.h"
#include "../town.h"
#include "../town_cmd.h"
#include "../timetable_cmd.h"
#include "../train.h"
#include "../train_cmd.h"
#include "../tree_cmd.h"
#include "../tunnelbridge_cmd.h"
#include "../tunnelbridge_map.h"
#include "../vehicle_cmd.h"
#include "../vehicle_func.h"
#include "../water_cmd.h"
#include "../waypoint_cmd.h"
#include "../waypoint_base.h"

#include "../depot_cmd.h"
#include "../economy_cmd.h"
#include <chrono>
#include <set>

#include "table/strings.h"

#include "../safeguards.h"

namespace Integration {
using namespace Detail;
namespace {

struct NativeDescription;
template <Commands Tcommand> NativeDescription DescribeNative(const CommandDataBuffer &data);

struct NativeCommandSpec {
	Commands command;
	NativeDescription (*describe)(const CommandDataBuffer &data);
};

template <Commands Tcommand>
constexpr NativeCommandSpec Native()
{
	return {Tcommand, DescribeNative<Tcommand>};
}

struct Spec {
	const char *name;
	std::initializer_list<const char *> required;
	std::initializer_list<const char *> optional;
	std::initializer_list<NativeCommandSpec> native = {};
};

/* Keep this allowlist independent from the native, revision-specific Commands enum. */
const Spec ACTIONS[] = {
	{"road.build", {"start", "end"}, {"road_type", "one_way"}, {Native<Commands::BuildRoadLong>(), Native<Commands::BuildRoad>()}},
	{"road.remove", {"start", "end"}, {"road_type"}, {Native<Commands::RemoveRoadLong>()}},
	{"road.convert", {"start", "end", "road_type"}, {}, {Native<Commands::ConvertRoad>()}},
	{"road.depot.build", {"tile", "direction"}, {"road_type"}, {Native<Commands::BuildRoadDepot>()}},
	{"road.stop.build", {"tile", "direction", "stop_type"}, {"road_type", "drive_through", "station_id", "adjacent", "width", "length", "spec_class", "spec_index"}, {Native<Commands::BuildRoadStop>()}},
	{"road.stop.remove", {"tile", "stop_type"}, {"width", "height", "remove_road"}, {Native<Commands::RemoveRoadStop>()}},
	{"road.waypoint.build", {"tile", "axis"}, {"road_type", "width", "height", "station_id", "adjacent", "spec_class", "spec_index"}, {Native<Commands::BuildRoadWaypoint>()}},
	{"rail.build", {"tile", "track"}, {"rail_type"}, {Native<Commands::BuildRail>()}},
	{"rail.remove", {"tile", "track"}, {}, {Native<Commands::RemoveRail>()}},
	{"rail.build_line", {"start", "end", "track"}, {"rail_type"}, {Native<Commands::BuildRailLong>()}},
	{"rail.remove_line", {"start", "end", "track"}, {}, {Native<Commands::RemoveRailLong>()}},
	{"rail.convert", {"start", "end", "rail_type"}, {}, {Native<Commands::ConvertRail>()}},
	{"rail.depot.build", {"tile", "direction"}, {"rail_type"}, {Native<Commands::BuildRailDepot>()}},
	{"rail.station.build", {"tile", "axis", "tracks", "length"}, {"rail_type", "station_id", "adjacent", "spec_class", "spec_index"}, {Native<Commands::BuildRailStation>()}},
	{"rail.station.remove", {"start", "end"}, {"keep_rail"}, {Native<Commands::RemoveFromRailStation>()}},
	{"rail.waypoint.build", {"tile", "axis"}, {"width", "height", "station_id", "adjacent", "spec_class", "spec_index"}, {Native<Commands::BuildRailWaypoint>()}},
	{"rail.waypoint.remove", {"start", "end"}, {"keep_rail"}, {Native<Commands::RemoveFromRailWaypoint>()}},
	{"rail.signal.build", {"tile", "track"}, {"signal_type", "semaphore", "direction_cycles", "convert"}, {Native<Commands::BuildSignal>(), Native<Commands::BuildSignalLong>()}},
	{"rail.signal.remove", {"tile", "track"}, {}, {Native<Commands::RemoveSignal>(), Native<Commands::RemoveSignalLong>()}},
	{"water.depot.build", {"tile", "axis"}, {}, {Native<Commands::BuildShipDepot>()}},
	{"water.dock.build", {"tile"}, {"station_id", "adjacent"}, {Native<Commands::BuildDock>()}},
	{"water.buoy.build", {"tile"}, {}, {Native<Commands::BuildBuoy>()}},
	{"water.lock.build", {"tile"}, {}, {Native<Commands::BuildLock>()}},
	{"water.canal.build", {"start", "end"}, {}, {Native<Commands::BuildCanal>()}},
	{"airport.build", {"tile", "airport_type"}, {"layout", "station_id", "adjacent"}, {Native<Commands::BuildAirport>()}},
	{"airport.open_close", {"station_id"}, {}, {Native<Commands::OpenCloseAirport>()}},
	{"station.rename", {"station_id", "name"}, {}, {Native<Commands::RenameStation>()}},
	{"vehicle.build", {"tile", "engine_id"}, {"cargo_id"}, {Native<Commands::BuildVehicle>()}},
	{"vehicle.clone", {"tile", "vehicle_id"}, {"share_orders"}, {Native<Commands::CloneVehicle>()}},
	{"vehicle.start_stop", {"vehicle_id"}, {}, {Native<Commands::StartStopVehicle>()}},
	{"vehicle.sell", {"vehicle_id"}, {"sell_chain"}, {Native<Commands::SellVehicle>()}},
	{"vehicle.refit", {"vehicle_id", "cargo_id"}, {"subtype", "count"}, {Native<Commands::RefitVehicle>()}},
	{"vehicle.depot", {"vehicle_id"}, {"service"}, {Native<Commands::SendVehicleToDepot>()}},
	{"vehicle.reverse", {"vehicle_id"}, {}, {Native<Commands::ReverseTrainDirection>(), Native<Commands::TurnRoadVehicle>()}},
	{"vehicle.rename", {"vehicle_id", "name"}, {}, {Native<Commands::RenameVehicle>()}},
	{"train.wagon.move", {"vehicle_id"}, {"destination_vehicle_id", "move_chain"}, {Native<Commands::MoveRailVehicle>()}},
	{"order.insert", {"vehicle_id", "order_version"}, {"destination", "position", "destination_type", "load", "unload", "routing", "stop_location", "depot_action", "non_stop", "service", "halt"}, {Native<Commands::InsertOrder>()}},
	{"order.delete", {"vehicle_id", "order_version", "position"}, {}, {Native<Commands::DeleteOrder>()}},
	{"order.skip", {"vehicle_id", "order_version", "position"}, {}, {Native<Commands::SkipToOrder>()}},
	{"order.move", {"vehicle_id", "order_version", "position", "target_position"}, {}, {Native<Commands::MoveOrder>()}},
	{"order.copy", {"vehicle_id", "order_version", "source_vehicle_id", "source_order_version"}, {"share"}, {Native<Commands::CloneOrder>()}},
	{"order.unshare", {"vehicle_id", "order_version"}, {"mode"}, {Native<Commands::CloneOrder>()}},
	{"terrain.raise", {"tile"}, {"corners"}, {Native<Commands::TerraformLand>()}},
	{"terrain.lower", {"tile"}, {"corners"}, {Native<Commands::TerraformLand>()}},
	{"terrain.level", {"start", "end"}, {}, {Native<Commands::LevelLand>()}},
	{"terrain.clear", {"tile"}, {}, {Native<Commands::LandscapeClear>(), Native<Commands::ClearArea>()}},
	{"terrain.plant_tree", {"tile"}, {"tree_type"}, {Native<Commands::PlantTree>()}},
	{"bridge.build", {"start", "end", "transport", "bridge_type"}, {"rail_type", "road_type"}, {Native<Commands::BuildBridge>()}},
	{"tunnel.build", {"tile", "transport"}, {"rail_type", "road_type"}, {Native<Commands::BuildTunnel>()}},
	{"company.loan.increase", {}, {"mode", "amount"}, {Native<Commands::IncreaseLoan>()}},
	{"company.loan.decrease", {}, {"mode", "amount"}, {Native<Commands::DecreaseLoan>()}},
	{"company.join_policy.set", {"policy"}, {}, {Native<Commands::CompanyAllowListControl>()}},
	{"company.participant.authorize", {"participant", "allowed"}, {}, {Native<Commands::CompanyAllowListControl>()}},
	{"company.rename", {"name"}, {}, {Native<Commands::RenameCompany>()}},
	{"company.give_money", {"amount", "company_id"}, {}, {Native<Commands::GiveMoney>()}},
	{"order.conditional.insert", {"vehicle_id", "order_version", "target_position", "variable"}, {"position", "comparator", "value"}, {Native<Commands::InsertOrder>()}},
	{"order.modify", {"vehicle_id", "order_version", "position", "field", "value"}, {}, {Native<Commands::ModifyOrder>()}},
	{"order.refit", {"vehicle_id", "order_version", "position", "mode"}, {"cargo_id"}, {Native<Commands::OrderRefit>()}},
	{"timetable.set", {"vehicle_id", "order_version", "position", "field", "value"}, {}, {Native<Commands::ChangeTimetable>()}},
	{"timetable.start", {"vehicle_id", "order_version", "start_tick"}, {"shared"}, {Native<Commands::SetTimetableStart>()}},
	{"timetable.autofill", {"vehicle_id", "order_version", "enabled"}, {"preserve_wait_time"}, {Native<Commands::AutofillTimetable>()}},
	{"timetable.reset_lateness", {"vehicle_id", "order_version"}, {"shared"}, {Native<Commands::SetVehicleOnTime>()}},
	{"group.create", {"vehicle_type"}, {"parent_group_id"}, {Native<Commands::CreateGroup>()}},
	{"group.delete", {"group_id"}, {}, {Native<Commands::DeleteGroup>()}},
	{"group.rename", {"group_id", "name"}, {}, {Native<Commands::AlterGroup>()}},
	{"group.parent.set", {"group_id"}, {"parent_group_id"}, {Native<Commands::AlterGroup>()}},
	{"group.vehicle.move", {"vehicle_id"}, {"group_id", "include_shared"}, {Native<Commands::AddVehicleToGroup>()}},
	{"group.protection.set", {"group_id", "enabled"}, {"recursive"}, {Native<Commands::SetGroupFlag>()}},
	{"group.wagon_removal.set", {"group_id", "enabled"}, {"recursive"}, {Native<Commands::SetGroupFlag>()}},
	{"group.livery.set", {"group_id", "channel", "colour"}, {}, {Native<Commands::SetGroupLivery>()}},
	{"autoreplace.set", {"scope", "old_engine_id", "new_engine_id"}, {"group_id", "when_old"}, {Native<Commands::SetAutoreplace>()}},
	{"autoreplace.remove", {"scope", "old_engine_id"}, {"group_id"}, {Native<Commands::SetAutoreplace>()}},
	{"vehicle.service_interval.set", {"vehicle_id", "mode"}, {"interval", "percent"}, {Native<Commands::ChangeServiceInterval>()}},
	{"train.force_proceed", {"vehicle_id"}, {}, {Native<Commands::ForceTrainProceed>()}},
	{"train.unit.flip", {"vehicle_id"}, {}, {Native<Commands::ReverseTrainDirection>()}},
	{"vehicle.autoreplace", {"vehicle_id"}, {}, {Native<Commands::AutoreplaceVehicle>()}},
	{"catalog.engine.visibility.set", {"engine_id", "hidden"}, {}, {Native<Commands::SetVehicleVisibility>()}},
	{"company.setting.set", {"key", "value"}, {}, {Native<Commands::ChangeCompanySetting>()}},
	{"object.build", {"tile", "object_type"}, {"view"}, {Native<Commands::BuildObject>(), Native<Commands::BuildObjectArea>()}},
	{"house.build", {"tile", "house_type"}, {"protected", "replace"}, {Native<Commands::PlaceHouse>(), Native<Commands::PlaceHouseArea>()}},
	{"town.found", {"tile"}, {"size", "city", "layout", "name"}, {Native<Commands::FoundTown>()}},
	{"town.action", {"town_id", "town_action"}, {}, {Native<Commands::TownAction>()}},
	{"industry.fund", {"industry_type"}, {"tile", "layout"}, {Native<Commands::BuildIndustry>()}},
	{"sign.place", {"tile", "name"}, {}, {Native<Commands::PlaceSign>()}},
	{"sign.rename", {"sign_id", "name"}, {}, {Native<Commands::RenameSign>()}},
	{"sign.delete", {"sign_id"}, {}, {Native<Commands::RenameSign>()}},
	{"sign.move", {"sign_id", "tile"}, {}, {Native<Commands::MoveSign>()}},
	{"depot.rename", {"depot_id", "name"}, {}, {Native<Commands::RenameDepot>()}},
	{"waypoint.rename", {"station_id", "name"}, {}, {Native<Commands::RenameWaypoint>()}},
	{"station.label.move", {"station_id", "tile"}, {}, {Native<Commands::MoveStationName>()}},
	{"waypoint.label.move", {"station_id", "tile"}, {}, {Native<Commands::MoveWaypointNAme>()}},
	{"road.waypoint.remove", {"start", "end"}, {}, {Native<Commands::RemoveFromRoadWaypoint>()}},
	{"road.pieces.build", {"tile", "road_bits"}, {"road_type", "toggle_directions"}, {Native<Commands::BuildRoad>()}},
	{"engine.preview.accept", {"engine_id"}, {}, {Native<Commands::WantEnginePreview>()}},
	{"company.president.rename", {"name"}, {}, {Native<Commands::RenamePresident>()}},
	{"company.livery.set", {"scheme", "channel", "colour"}, {}, {Native<Commands::SetCompanyColour>()}},
	{"company.buy", {"company_id"}, {}, {Native<Commands::BuyCompany>()}},
	{"company.manager_face.style.set", {"style_id"}, {}, {Native<Commands::SetCompanyManagerFace>()}},
	{"company.manager_face.component.set", {"expected_style_id", "component_id", "value"}, {}, {Native<Commands::SetCompanyManagerFace>()}},
	{"goal.question.answer", {"question_id", "button"}, {}, {Native<Commands::GoalQuestionAnswer>()}},
	{"story.button.press", {"element_id", "page_version"}, {}, {Native<Commands::StoryPageButton>()}},
	{"story.button.select_tile", {"element_id", "page_version", "tile"}, {}, {Native<Commands::StoryPageButton>()}},
	{"story.button.select_vehicle", {"element_id", "page_version", "vehicle_id"}, {}, {Native<Commands::StoryPageButton>()}},
};

const Spec QUERIES[] = {
	{"map.region", {"x", "y", "width", "height"}, {"after_offset", "limit"}},
	{"company", {}, {}},
	{"company.access", {}, {"participant"}},
	{"vehicles", {}, {"after_id", "limit"}},
	{"stations", {}, {"after_id", "limit"}},
	{"orders", {"vehicle_id"}, {"after_position", "limit", "shared_after_id", "shared_limit", "include_live_detail"}},
	{"catalog.engines", {}, {"after_id", "limit", "vehicle_type"}},
	{"catalog.airports", {}, {}},
	{"catalog.cargo", {}, {}},
	{"cargo.payment_rates", {"cargo_id"}, {}},
	{"catalog.bridges", {}, {}},
	{"engine.purchase_info", {"engine_id"}, {}},
	{"catalog.railtypes", {}, {}},
	{"catalog.roadtypes", {}, {}},
	{"towns", {}, {"after_id", "limit"}},
	{"industries", {}, {"after_id", "limit"}},
	{"industry.details", {"industry_id"}, {}},
	{"industry.monthly_stats", {"industry_id", "cargo_id", "direction"}, {}},
	{"timetable", {"vehicle_id"}, {"after_position", "limit", "shared_after_id", "shared_limit", "include_live_detail"}},
	{"groups", {}, {"after_id", "limit"}},
	{"group.stats", {"scope"}, {"group_id", "vehicle_type", "engine_id"}},
	{"autoreplace", {"scope"}, {"group_id", "after_id", "limit"}},
	{"catalog.objects", {}, {"after_id", "limit"}},
	{"catalog.houses", {}, {"after_id", "limit"}},
	{"catalog.industries", {}, {"after_id", "limit"}},
	{"catalog.trees", {}, {"after_id", "limit"}},
	{"catalog.station_specs", {}, {"after_id", "limit", "kind"}},
	{"catalog.roadstop_specs", {}, {"after_id", "limit", "kind", "road_type"}},
	{"depots", {}, {"after_id", "limit"}},
	{"waypoints", {}, {"after_id", "limit"}},
	{"signs", {}, {"after_id", "limit"}},
	{"company.appearance", {}, {}},
	{"catalog.manager_faces", {}, {"after_id", "limit", "style_id"}},
	{"company.report", {}, {"company_id"}},
	{"engine.preview_offers", {}, {"after_id", "limit"}},
	{"world", {}, {}},
	{"land.info", {"tile"}, {}},
	{"station.site", {"build_action", "build_parameters"}, {}},
	{"town.authority", {"town_id"}, {}},
	{"goal.questions", {}, {"after_id", "after_generation", "limit"}},
	{"goal.question", {"question_id"}, {"text_offset", "text_limit"}},
	{"news.custom", {}, {"after", "limit"}},
	{"story.pages", {}, {"book_company_id", "after_id", "limit"}},
	{"story.elements", {"page_id"}, {"after_id", "limit", "text_offset", "text_limit"}},
	{"story.element", {"element_id"}, {"text_offset", "text_limit"}},
	{"story.vehicle_candidates", {"element_id", "page_version"}, {"after_id", "limit"}},
	{"town.detail", {"town_id"}, {}},
	{"subsidies", {}, {"after_id", "limit"}},
	{"league.performance", {}, {}},
	{"league.tables", {}, {"after_id", "limit"}},
	{"league.table.entries", {"table_id"}, {"after_id", "limit"}},
	{"goals", {}, {"scope", "after_id", "limit"}},
	{"station.details", {"station_id"}, {}},
	{"station.cargo.flows", {"station_id", "cargo_id"}, {"after_source_id", "after_share_cumulative", "limit"}},
	{"station.cargo.waiting", {"station_id", "cargo_id"}, {"after_group", "aggregate_version", "limit"}},
	{"station.links", {"station_id", "cargo_id"}, {"after_offset", "limit"}},
	{"vehicle.details", {"vehicle_id"}, {"after_part_position", "part_limit", "consist_version"}},
	{"vehicle.refit_subtypes", {"vehicle_id", "part_id", "cargo_id"}, {"after_subtype", "limit"}},
	{"company.valuation", {}, {"company_id"}},
	{"display.text", {"kind"}, {"target", "cargo_id", "vehicle_id", "subtype", "direction", "slot", "after_byte", "limit", "expected_content_version"}},
};

Json IntegerSchema(int64_t minimum, int64_t maximum)
{
	return {{"type", "integer"}, {"minimum", minimum}, {"maximum", maximum}};
}

Json EnumSchema(std::initializer_list<const char *> values)
{
	return {{"type", "string"}, {"enum", values}};
}


Json StringEnumSchema(std::span<const std::string_view> values, bool inherit = false)
{
 Json choices = Json::array();
 for (auto value : values) choices.push_back(value);
 if (inherit) choices.push_back("inherit");
 return {{"type", "string"}, {"enum", std::move(choices)}};
}

Json EntityHandleSchema(uint32_t maximum)
{
 return {{"type", "object"}, {"required", {"id", "generation", "engine_instance_id"}}, {"additionalProperties", false},
  {"properties", {{"id", IntegerSchema(0, maximum)}, {"generation", {{"type", "integer"}, {"minimum", 1}, {"maximum", UINT64_MAX}}},
   {"engine_instance_id", {{"type", "string"}, {"minLength", 1}, {"maxLength", 256}}}}}};
}

Json NewsCursorSchema(bool item = false)
{
	Json schema = EntityHandleSchema(0);
	schema["properties"]["id"] = IntegerSchema(item ? 1 : 0, INT64_MAX);
	return schema;
}

/** One field schema source is used for capability publication and admission. */
Json FieldSchema(const Spec &spec, std::string_view key)
{
	std::string_view name(spec.name);

 if (key == "page_id" || key == "element_id") return EntityHandleSchema(63999);
 if (key == "question_id") return EntityHandleSchema(65535);
 if (key == "page_version") return {{"type", "string"}, {"minLength", 69}, {"maxLength", 69}};
 if (key == "book_company_id") return {{"anyOf", Json::array({IntegerSchema(0, MAX_COMPANIES - 1), Json{{"type", "null"}}})}};
 if (key == "text_offset") return IntegerSchema(0, INT32_MAX);
 if (key == "text_limit") return IntegerSchema(1, 8192);
 if (key == "limit" && (name.starts_with("story.") || name == "goal.questions" || name == "news.custom")) return IntegerSchema(1, 32);
 if (key == "after" && name == "news.custom") return NewsCursorSchema();
 if (key == "after_id" && name == "goal.questions") return IntegerSchema(-1, 65535);
 if (key == "after_generation" && name == "goal.questions") return {{"type", "integer"}, {"minimum", 0}, {"maximum", UINT64_MAX}};
 if (key == "after_subtype") return IntegerSchema(-1, 254);
 if (key == "limit" && name == "vehicle.refit_subtypes") return IntegerSchema(1, 16);
 if (key == "button") return EnumSchema({"cancel", "ok", "no", "yes", "decline", "accept", "ignore", "retry", "previous", "next", "stop", "start", "go", "continue", "restart", "postpone", "surrender", "close"});
 if (key == "table_id") return EntityHandleSchema(254);
 if (key == "industry_id") return EntityHandleSchema(63999);
 if (key == "direction" && name == "industry.monthly_stats") return EnumSchema({"accepted", "produced"});
 if (key == "target" && name == "display.text") return EntityHandleSchema(0xFEFFF);
 if (key == "direction" && name == "display.text") return EnumSchema({"accepted", "produced"});
 if (key == "slot" && name == "display.text") return IntegerSchema(0, 15);
 if (key == "subtype" && name == "display.text") return IntegerSchema(0, 254);
 if (key == "after_byte") return {{"type", "integer"}, {"minimum", 0}, {"maximum", UINT64_MAX}};
 if (key == "expected_content_version") return {{"type", "string"}, {"minLength", 64}, {"maxLength", 64}};
 if (key == "limit" && name == "display.text") return IntegerSchema(1, 2048);
 if (key == "kind" && name == "display.text") return EnumSchema({"town_text", "cargo_name", "subsidy_cargo_name", "subsidy_source_name", "subsidy_destination_name", "league_table_title", "league_table_header", "league_table_footer", "league_entry_text", "league_entry_score", "goal_text", "goal_progress", "industry_cargo_line", "industry_extra_text", "industry_custom_text", "industry_name", "vehicle_refit_label", "news_custom_text"});
 if (key == "consist_version") return {{"type", "string"}, {"minLength", 68}, {"maxLength", 68}};
 if (key == "after_part_position") return IntegerSchema(-1, 4095);
 if (key == "part_limit") return IntegerSchema(1, 16);
 if (key == "include_live_detail") return {{"type", "boolean"}};
 if (key == "after_source_id") return IntegerSchema(-1, 65535);
 if (key == "after_share_cumulative") return IntegerSchema(0, UINT32_MAX);
 if (key == "after_group" && name == "station.cargo.waiting") return {{"type", "object"}, {"required", {"first_station_id", "next_hop_id", "packet_units"}}, {"additionalProperties", false}, {"properties", {{"first_station_id", IntegerSchema(0, 65535)}, {"next_hop_id", {{"anyOf", Json::array({IntegerSchema(0, 65535), Json{{"type", "null"}}})}}}, {"packet_units", IntegerSchema(1, UINT16_MAX)}}}};
 if (key == "aggregate_version" && name == "station.cargo.waiting") return {{"type", "string"}, {"minLength", 69}, {"maxLength", 69}, {"pattern", "^swh1:[0-9a-f]{64}$"}};
 if (key == "scope" && name == "goals") return EnumSchema({"global", "company"});
 if (key == "after_id" && name == "subsidies") return IntegerSchema(-1, 255);
 if (key == "after_id" && name == "league.tables") return IntegerSchema(-1, 254);
 if (key == "after_id" && (name == "goals" || name == "league.table.entries")) return IntegerSchema(-1, 63999);
 if (key == "after_offset" && name == "station.links") return IntegerSchema(-1, 65535);
 if (key == "limit" && (name == "subsidies" || name.starts_with("league.") || name == "goals" || name.starts_with("station."))) return IntegerSchema(1, 64);
 if (key == "participant") return EntityHandleSchema(UINT32_MAX);
 if (key == "policy" && name == "company.join_policy.set") return EnumSchema({"any", "listed"});
 if (key == "allowed" && name == "company.participant.authorize") return {{"type", "boolean"}};
 if (key == "amount" && name.starts_with("company.loan.")) return IntegerSchema(1, INT64_MAX);
 if (key == "group_id" || key == "parent_group_id" || key == "town_id" || key == "sign_id" || key == "depot_id") return EntityHandleSchema(63999);
 if (key == "company_id" && name == "company.buy") return EntityHandleSchema(MAX_COMPANIES - 1);
 if (key == "order_version" || key == "source_order_version") return {{"type", "string"}, {"minLength", 68}, {"maxLength", 68}};
 if (key == "after_position") return IntegerSchema(-1, 253);
 if (key == "shared_after_id") return IntegerSchema(-1, UINT32_MAX);
 if (key == "shared_limit") return IntegerSchema(1, 64);
 if (key == "limit" && (name == "orders" || name == "timetable")) return IntegerSchema(1, 64);
 if (key == "limit" && (name == "groups" || name == "autoreplace")) return IntegerSchema(1, 100);
 if (key == "limit" && name == "engine.preview_offers") return IntegerSchema(1, 64);
 if (key == "limit" && name == "catalog.manager_faces") return IntegerSchema(1, 16);
 if (key == "position") return IntegerSchema(0, name == "order.insert" || name == "order.conditional.insert" ? 254 : 253);
 if (key == "target_position" || key == "condition_target_position") return IntegerSchema(0, 253);
 if (key == "start_tick") return {{"type", "integer"}, {"minimum", 0}, {"maximum", UINT64_MAX}};
 if (key == "subtype") return {{"anyOf", Json::array({IntegerSchema(0, 254), EnumSchema({"keep"})})}};
 if (key == "count") return IntegerSchema(1, 255);
 if (key == "scope") return EnumSchema({"all", "default", "group"});
 if (key == "channel") return EnumSchema({"primary", "secondary"});
 if (key == "colour") return StringEnumSchema(Detail::CompanyColourNames(), true);
 if (key == "scheme") return StringEnumSchema(Detail::CompanyLiveryNames());
 if (key == "mode") {
  if (name.starts_with("company.loan.")) return EnumSchema({"amount", "interval", "max"});
  if (name == "order.unshare") return EnumSchema({"keep", "clear"});
  if (name == "order.refit") return EnumSchema({"cargo", "auto", "none"});
  if (name == "vehicle.service_interval.set") return EnumSchema({"default", "custom"});
  throw InvalidParameters("Missing mode schema for capability: " + std::string(name));
 }
 if (key == "engine_id" && name == "engine.preview.accept") return IntegerSchema(0, UINT16_MAX);
 if (key == "old_engine_id" || key == "new_engine_id") return IntegerSchema(0, 63999);
 if (key == "interval") return IntegerSchema(1, UINT16_MAX);
 if (key == "routing") return EnumSchema({"stop", "non_stop", "via", "non_stop_via"});
 if (key == "stop_location") return EnumSchema({"near", "middle", "far"});
 if (key == "depot_action") return EnumSchema({"always", "service", "halt", "unbunch"});
 if (key == "variable" || key == "condition_variable") return EnumSchema({"load_percentage", "reliability", "max_reliability", "max_speed", "age", "remaining_lifetime", "requires_service", "unconditional", "driving_backwards"});
 if (key == "comparator" || key == "condition_comparator") return EnumSchema({"eq", "ne", "lt", "le", "gt", "ge", "is_true", "is_false"});
 if (key == "condition_value") return IntegerSchema(0, 2047);
 if (key == "field") return name == "timetable.set" ? EnumSchema({"wait_ticks", "travel_ticks", "speed_limit"}) : EnumSchema({"load", "unload", "routing", "stop_location", "depot_action", "condition_variable", "condition_comparator", "condition_value", "condition_target_position"});
 if (key == "key") return EnumSchema({"autorenew_enabled", "autorenew_months", "autorenew_minimum_money", "renew_keep_length", "service_percent", "service_trains", "service_road", "service_ships", "service_aircraft"});
 if (key == "value") {
  if (name == "timetable.set") return IntegerSchema(0, UINT16_MAX);
  if (name == "order.conditional.insert") return IntegerSchema(0, 2047);
  if (name == "company.manager_face.component.set") return IntegerSchema(0, 255);
  if (name == "company.setting.set") return {{"anyOf", Json::array({Json{{"type", "boolean"}}, IntegerSchema(-12, 2000000)})}};
  if (name == "order.modify") return {{"anyOf", Json::array({IntegerSchema(0, 2047), EnumSchema({"if_possible", "full", "full_any", "none", "all", "transfer", "stop", "non_stop", "via", "non_stop_via", "near", "middle", "far", "always", "service", "halt", "unbunch", "load_percentage", "reliability", "max_reliability", "max_speed", "age", "remaining_lifetime", "requires_service", "unconditional", "driving_backwards", "eq", "ne", "lt", "le", "gt", "ge", "is_true", "is_false"})})}};
  throw InvalidParameters("Missing value schema for capability: " + std::string(name));
 }
 if (key == "object_type") return IntegerSchema(0, 63999);
 if (key == "house_type") return IntegerSchema(0, 4095);
 if (key == "industry_type") return IntegerSchema(0, 239);
 if (key == "tree_type") return IntegerSchema(0, 40);
 if (key == "view") return IntegerSchema(0, 255);
 if (key == "road_bits") return IntegerSchema(1, 15);
 if (key == "spec_class" || key == "spec_index") return IntegerSchema(0, 65534);
 if (key == "toggle_directions") return EnumSchema({"none", "northbound", "southbound", "both"});
 if (key == "size") return EnumSchema({"small", "medium", "random"});
 if (key == "layout" && name == "town.found") return EnumSchema({"original", "better_roads", "grid_2x2", "grid_3x3", "random"});
 if (key == "layout" && name == "industry.fund") return IntegerSchema(0, UINT32_MAX);
 if (key == "town_action") return EnumSchema({"advertise_small", "advertise_medium", "advertise_large", "road_rebuild", "build_statue", "fund_buildings", "buy_rights", "bribe"});
 if (key == "kind") return name == "catalog.station_specs" ? EnumSchema({"station", "waypoint"}) : EnumSchema({"bus", "truck", "waypoint"});
 if (key == "style_id" || key == "expected_style_id") return IntegerSchema(0, UINT32_MAX);
 if (key == "component_id") return IntegerSchema(0, 63);
	if (key == "tile" || key == "start" || key == "end" || key == "destination") {
		return {{"anyOf", Json::array({IntegerSchema(0, UINT32_MAX - 1), {{"type", "object"}, {"required", {"x", "y"}}, {"additionalProperties", false}, {"properties", {{"x", IntegerSchema(0, UINT32_MAX)}, {"y", IntegerSchema(0, UINT32_MAX)}}}}})}};
	}
	if (key == "vehicle_id" || key == "source_vehicle_id" || key == "destination_vehicle_id" || key == "part_id" || key == "station_id") {
		return {{"type", "object"}, {"required", {"id", "generation", "engine_instance_id"}}, {"additionalProperties", false},
			{"properties", {{"id", IntegerSchema(0, key == "station_id" ? 63999 : 0xFEFFF)},
				{"generation", {{"type", "integer"}, {"minimum", 1}, {"maximum", UINT64_MAX}}},
				{"engine_instance_id", {{"type", "string"}, {"minLength", 1}, {"maxLength", 256}}}}}};
	}
	if (key == "axis") return EnumSchema({"x", "y"});
	if (key == "track") return EnumSchema({"x", "y", "upper", "lower", "left", "right"});
	if (key == "stop_type") return EnumSchema({"bus", "truck"});
	if (key == "signal_type") return EnumSchema({"block", "entry", "exit", "combo", "path", "oneway_path"});
	if (key == "destination_type") return EnumSchema({"station", "waypoint", "depot", "nearest_depot"});
	if (key == "load") return EnumSchema({"if_possible", "full", "full_any", "none"});
	if (key == "unload") return EnumSchema({"if_possible", "all", "transfer", "none"});
	if (key == "transport") return name == "bridge.build" ? EnumSchema({"road", "rail", "water"}) : EnumSchema({"road", "rail"});
	if (key == "vehicle_type") return EnumSchema({"road", "rail", "ship", "aircraft"});
	if (key == "name") return {{"type", "string"}, {"maxLength", 128}};
	if (key == "road_type") return IntegerSchema(0, ROADTYPE_END - 1);
	if (key == "rail_type") return IntegerSchema(0, RAILTYPE_END - 1);
	if (key == "engine_id") return IntegerSchema(0, 63999);
	if (key == "airport_type") return IntegerSchema(0, NUM_AIRPORTS - 1);
	if (key == "bridge_type") return IntegerSchema(0, MAX_BRIDGES - 1);
	if (key == "layout") return IntegerSchema(0, 255);
	if (key == "direction" && (name == "road.depot.build" || name == "rail.depot.build")) return {{"anyOf", Json::array({IntegerSchema(0, 3), EnumSchema({"auto"})})}};
	if (key == "direction" || key == "direction_cycles") return IntegerSchema(0, 3);
	if (key == "cargo_id") return IntegerSchema(0, NUM_CARGO - 1);
	if (key == "position" || key == "target_position") return IntegerSchema(0, MAX_VEH_ORDER_ID);
	if (key == "corners") return IntegerSchema(1, 15);
	if (key == "amount") return IntegerSchema(1, INT32_MAX);
	if (key == "company_id") return IntegerSchema(0, MAX_COMPANIES - 1);
	if (key == "after_id") return IntegerSchema(-1, UINT32_MAX);
	if (key == "after_offset") return IntegerSchema(-1, 4095);
	if (key == "limit" && name == "map.region") return IntegerSchema(1, 100);
	if (key == "limit") return IntegerSchema(1, 256);
	if (key == "x" || key == "y") return IntegerSchema(0, UINT32_MAX);
	if (key == "width" || key == "height") return IntegerSchema(1, 64);
	if (key == "tracks" || key == "length") return IntegerSchema(1, NATIVE_MAX_STATION_EXTENT);
 for (const char *boolean : {"one_way", "drive_through", "adjacent", "remove_road", "keep_rail", "semaphore", "convert", "share_orders", "sell_chain", "service", "share", "halt", "move_chain", "non_stop", "enabled", "recursive", "include_shared", "when_old", "percent", "hidden", "shared", "preserve_wait_time", "protected", "replace", "city"}) {
  if (key == boolean) return {{"type", "boolean"}};
 }
 throw InvalidParameters("Missing field schema for capability " + std::string(name) + ": " + std::string(key));
}

Json ParameterSchema(const Spec &spec)
{
 if (std::string_view(spec.name) == "station.site") {
  Json variants = Json::array();
  for (const char *action : {"rail.station.build", "road.stop.build", "water.dock.build", "airport.build"}) {
   const auto selected = std::ranges::find(ACTIONS, std::string_view(action), &Spec::name);
   if (selected == std::end(ACTIONS)) throw InvalidParameters("Station site action unavailable");
   Json parameters = ParameterSchema(*selected);
   variants.push_back({{"type", "object"}, {"required", {"build_action", "build_parameters"}}, {"additionalProperties", false},
    {"properties", {{"build_action", EnumSchema({action})}, {"build_parameters", std::move(parameters)}}}});
  }
  return {{"anyOf", std::move(variants)}};
 }
 Json required = Json::array(), properties = Json::object();
 for (const char *key : spec.required) { required.push_back(key); properties[key] = FieldSchema(spec, key); }
 for (const char *key : spec.optional) properties[key] = FieldSchema(spec, key);
 Json schema = {{"type", "object"}, {"required", required}, {"additionalProperties", false}, {"properties", properties}};
 std::string_view name(spec.name);
 auto branch = [&](std::initializer_list<const char *> extra_required, std::initializer_list<const char *> forbidden, Json narrow) {
  Json value = schema;
  for (const char *key : extra_required) if (std::ranges::find(value["required"], key) == value["required"].end()) value["required"].push_back(key);
  for (const char *key : forbidden) value["properties"].erase(key);
  value["properties"].update(narrow);
  return value;
 };
 auto singleton = [](std::string_view value) { return Json{{"type", "string"}, {"enum", Json::array({std::string(value)})}}; };
 if (name == "goal.questions") return {{"anyOf", Json::array({branch({}, {}, {{"after_id", IntegerSchema(-1, -1)}, {"after_generation", IntegerSchema(0, 0)}}), branch({"after_id"}, {}, {{"after_id", IntegerSchema(0, 65535)}})})}};
 if (name == "display.text") {
  Json variants = Json::array();
  auto add_offsets = [&](Json value) {
   Json first = value;
   first["properties"]["after_byte"] = IntegerSchema(0, 0);
   variants.push_back(std::move(first));
   value["required"].push_back("after_byte");
   value["required"].push_back("expected_content_version");
   value["properties"]["after_byte"] = {{"type", "integer"}, {"minimum", 1}, {"maximum", UINT64_MAX}};
   variants.push_back(std::move(value));
  };
  add_offsets(branch({"cargo_id"}, {"target", "vehicle_id", "subtype", "direction", "slot"}, {{"kind", singleton("cargo_name")}}));
  add_offsets(branch({"target"}, {"cargo_id", "vehicle_id", "subtype", "direction", "slot"}, {{"kind", singleton("news_custom_text")}, {"target", NewsCursorSchema(true)}}));
  add_offsets(branch({"target"}, {"cargo_id", "vehicle_id", "subtype", "direction", "slot"}, {{"kind", EnumSchema({"town_text", "league_entry_text", "league_entry_score", "goal_text", "goal_progress", "industry_extra_text", "industry_custom_text", "industry_name"})}, {"target", EntityHandleSchema(63999)}}));
  add_offsets(branch({"target"}, {"cargo_id", "vehicle_id", "subtype", "direction", "slot"}, {{"kind", EnumSchema({"subsidy_cargo_name", "subsidy_source_name", "subsidy_destination_name"})}, {"target", EntityHandleSchema(255)}}));
  add_offsets(branch({"target"}, {"cargo_id", "vehicle_id", "subtype", "direction", "slot"}, {{"kind", EnumSchema({"league_table_title", "league_table_header", "league_table_footer"})}, {"target", EntityHandleSchema(254)}}));
  add_offsets(branch({"target", "cargo_id", "direction", "slot"}, {"vehicle_id", "subtype"}, {{"kind", singleton("industry_cargo_line")}, {"target", EntityHandleSchema(63999)}}));
  add_offsets(branch({"target", "vehicle_id", "cargo_id", "subtype"}, {"direction", "slot"}, {{"kind", singleton("vehicle_refit_label")}, {"target", EntityHandleSchema(0xFEFFF)}}));
  return {{"anyOf", std::move(variants)}};
 }
 if (name == "orders" || name == "timetable") return {{"anyOf", Json::array({branch({}, {}, {{"include_live_detail", {{"type", "boolean"}, {"enum", Json::array({false})}}}}), branch({"include_live_detail"}, {}, {{"include_live_detail", {{"type", "boolean"}, {"enum", Json::array({true})}}}, {"limit", IntegerSchema(1, 16)}, {"shared_limit", IntegerSchema(1, 16)}})})}};
 if (name == "station.cargo.flows") return {{"anyOf", Json::array({branch({}, {"after_share_cumulative"}, {{"after_source_id", IntegerSchema(-1, -1)}}), branch({"after_source_id"}, {}, {{"after_source_id", IntegerSchema(0, 65535)}})})}};
 if (name == "station.cargo.waiting") return {{"anyOf", Json::array({branch({}, {"after_group", "aggregate_version"}, Json::object()), branch({"after_group"}, {"aggregate_version"}, Json::object()), branch({"after_group", "aggregate_version"}, {}, Json::object())})}};
 if (name == "vehicle.details") return {{"anyOf", Json::array({branch({}, {}, {{"after_part_position", IntegerSchema(-1, -1)}}), branch({"after_part_position", "consist_version"}, {}, {{"after_part_position", IntegerSchema(0, 4095)}})})}};
 if (name == "company.loan.increase" || name == "company.loan.decrease") return {{"anyOf", Json::array({branch({"amount"}, {}, {{"mode", singleton("amount")}}), branch({"mode"}, {"amount"}, {{"mode", EnumSchema({"interval", "max"})}})})}};
 if (name == "order.modify") {
  Json variants = Json::array();
  for (const char *field : {"load", "unload", "routing", "stop_location", "depot_action", "condition_variable", "condition_comparator", "condition_value", "condition_target_position"}) variants.push_back(branch({}, {}, {{"field", singleton(field)}, {"value", FieldSchema(spec, field)}}));
  return {{"anyOf", std::move(variants)}};
 }
 if (name == "company.setting.set") {
  Json variants = Json::array();
  for (const char *key : {"autorenew_enabled", "renew_keep_length", "service_percent"}) variants.push_back(branch({}, {}, {{"key", singleton(key)}, {"value", {{"type", "boolean"}}}}));
  variants.push_back(branch({}, {}, {{"key", singleton("autorenew_months")}, {"value", IntegerSchema(-12, 12)}}));
  variants.push_back(branch({}, {}, {{"key", singleton("autorenew_minimum_money")}, {"value", IntegerSchema(0, 2000000)}}));
  for (const char *key : {"service_trains", "service_road", "service_ships", "service_aircraft"}) variants.push_back(branch({}, {}, {{"key", singleton(key)}, {"value", IntegerSchema(0, 800)}}));
  return {{"anyOf", std::move(variants)}};
 }
 if (name == "order.refit") return {{"anyOf", Json::array({branch({"cargo_id"}, {}, {{"mode", singleton("cargo")}}), branch({}, {"cargo_id"}, {{"mode", EnumSchema({"auto", "none"})}})})}};
 if (name == "vehicle.service_interval.set") return {{"anyOf", Json::array({branch({"interval", "percent"}, {}, {{"mode", singleton("custom")}}), branch({}, {"interval", "percent"}, {{"mode", singleton("default")}})})}};
 if (name == "autoreplace.set" || name == "autoreplace.remove" || name == "autoreplace" || name == "group.stats") {
  return {{"anyOf", Json::array({branch({"group_id"}, name == "group.stats" ? std::initializer_list<const char *>{"vehicle_type"} : std::initializer_list<const char *>{}, {{"scope", singleton("group")}}), branch(name == "group.stats" ? std::initializer_list<const char *>{"vehicle_type"} : std::initializer_list<const char *>{}, {"group_id"}, {{"scope", EnumSchema({"all", "default"})}})})}};
 }
 if (name == "order.insert") {
  // The native family additionally enforces policy exclusivity and current vehicle compatibility.
  return {{"anyOf", Json::array({branch({"destination"}, {"depot_action", "service", "halt"}, {{"destination_type", singleton("station")}}), branch({"destination", "destination_type"}, {"load", "unload", "stop_location", "depot_action", "service", "halt"}, {{"destination_type", singleton("waypoint")}}), branch({"destination", "destination_type"}, {"load", "unload", "stop_location"}, {{"destination_type", singleton("depot")}}), branch({"destination_type"}, {"destination", "load", "unload", "stop_location"}, {{"destination_type", singleton("nearest_depot")}})})}};
 }
 return schema;
}

bool MatchesSchema(const Json &schema, const Json &value)
{
	if (schema.contains("anyOf")) return std::ranges::any_of(schema["anyOf"], [&](const Json &candidate) { return MatchesSchema(candidate, value); });
	auto type = schema.at("type").get<std::string>();
	if (type == "null") return value.is_null();
	if (type == "integer") {
		if (!value.is_number_integer()) return false;
		if (value.is_number_unsigned()) {
			auto number = value.get<uint64_t>();
			if (schema.contains("maximum") && !schema["maximum"].is_number_unsigned() && schema["maximum"].get<int64_t>() < 0) return false;
			return (!schema.contains("minimum") || (schema["minimum"].get<int64_t>() <= 0 || number >= schema["minimum"].get<uint64_t>())) && (!schema.contains("maximum") || number <= schema["maximum"].get<uint64_t>());
		}
		auto number = value.get<int64_t>();
		if (schema.contains("minimum") && number < schema["minimum"].get<int64_t>()) return false;
		return !schema.contains("maximum") || (schema["maximum"].is_number_unsigned() && schema["maximum"].get<uint64_t>() > INT64_MAX) || number <= schema["maximum"].get<int64_t>();
	}
	if (type == "boolean") return value.is_boolean() && (!schema.contains("enum") || std::ranges::find(schema["enum"], value) != schema["enum"].end());
	if (type == "string") {
		if (!value.is_string()) return false;
		auto text = value.get<std::string>();
		if (text.find('\0') != std::string::npos || (schema.contains("minLength") && text.size() < schema["minLength"].get<size_t>()) ||
				(schema.contains("maxLength") && text.size() > schema["maxLength"].get<size_t>())) return false;
		return !schema.contains("enum") || std::ranges::find(schema["enum"], value) != schema["enum"].end();
	}
	if (type == "object") {
		if (!value.is_object()) return false;
		for (const auto &key : schema.at("required")) if (!value.contains(key.get<std::string>())) return false;
		for (auto item = value.begin(); item != value.end(); ++item) {
			if (!schema.at("properties").contains(item.key()) || !MatchesSchema(schema["properties"][item.key()], item.value())) return false;
		}
		return true;
	}
	return false;
}

const Spec &ValidateShape(std::span<const Spec> specs, const std::string &name, const Json &parameters)
{
	auto spec = std::ranges::find(specs, name, &Spec::name);
	if (spec == specs.end()) throw InvalidParameters("Unknown capability: " + name);
	if (!MatchesSchema(ParameterSchema(*spec), parameters)) throw InvalidParameters("Parameters do not match capability schema: " + name);
	return *spec;
}

Json VehicleSnapshot(const Vehicle *vehicle, size_t &part_budget)
{
	TileIndex location = vehicle->tile;
	if (vehicle->type == VehicleType::Aircraft) location = TileXY(Clamp(vehicle->x_pos / static_cast<int>(TILE_SIZE), 0, static_cast<int>(Map::MaxX())), Clamp(vehicle->y_pos / static_cast<int>(TILE_SIZE), 0, static_cast<int>(Map::MaxY())));
	Json result = {{"vehicle_id", Handle(vehicle)}, {"vehicle_type", VehicleTypeName(vehicle->type)}, {"engine_id", vehicle->engine_type.base()},
		{"location", TileCoordinates(location)}, {"speed", vehicle->GetDisplaySpeed()}, {"maximum_speed", vehicle->GetDisplayMaxSpeed()},
		{"stopped", vehicle->vehstatus.Test(VehState::Stopped)}, {"crashed", vehicle->vehstatus.Test(VehState::Crashed)},
		{"profit_this_year", vehicle->GetDisplayProfitThisYear().base()}, {"profit_last_year", vehicle->GetDisplayProfitLastYear().base()},
		{"running_cost", vehicle->GetDisplayRunningCost().base()}, {"order_count", vehicle->GetNumOrders()}, {"current_order", vehicle->cur_real_order_index}};
	const Group *group = Group::GetIfValid(vehicle->group_id);
	result["group_id"] = group == nullptr ? Json(nullptr) : Handle(group);
	result["service_interval"] = vehicle->GetServiceInterval();
	result["service_custom"] = vehicle->ServiceIntervalIsCustom();
	result["service_percent"] = vehicle->ServiceIntervalIsPercent();
	if (vehicle->type == VehicleType::Train) result["force_proceed"] = to_underlying(Train::From(vehicle)->force_proceed);
	Json parts = Json::array();
	const Vehicle *part = vehicle;
	for (; part != nullptr && part_budget > 0; part = part->Next(), --part_budget) {
		parts.push_back({{"vehicle_id", Handle(part)}, {"engine_id", part->engine_type.base()}, {"cargo_id", to_underlying(part->cargo_type)}, {"capacity", part->cargo_cap}, {"load", part->cargo.StoredCount()}});
	}
	result["parts"] = std::move(parts);
	result["parts_truncated"] = part != nullptr;
	return result;
}

Json StationSnapshot(const Station *station)
{
	Json result = {{"station_id", Handle(station)}, {"name", GetString(STR_STATION_NAME, station->index)}, {"location", TileCoordinates(station->xy)},
		{"rail", station->facilities.Test(StationFacility::Train)}, {"bus", station->facilities.Test(StationFacility::BusStop)},
		{"truck", station->facilities.Test(StationFacility::TruckStop)}, {"dock", station->facilities.Test(StationFacility::Dock)},
		{"airport", station->facilities.Test(StationFacility::Airport)}};
	Json cargo = Json::array();
	for (uint id = 0; id < NUM_CARGO; ++id) {
		if (!CargoSpec::Get(id)->IsValid()) continue;
		const auto &goods = station->goods[static_cast<CargoType>(id)];
		cargo.push_back({{"cargo_id", id}, {"waiting", goods.TotalCount()}, {"has_rating", goods.HasRating()}, {"rating", goods.rating}});
	}
	result["cargo"] = std::move(cargo);
	if (station->facilities.Test(StationFacility::Airport)) {
		Json hangars = Json::array();
		for (uint i = 0; i < station->airport.GetNumHangars(); ++i) hangars.push_back(TileCoordinates(station->airport.GetHangarTile(i)));
		result["airport_details"] = {{"airport_type", station->airport.type}, {"layout", station->airport.layout}, {"hangars", std::move(hangars)}};
	}
	return result;
}

/** Private, typed native references. Never serialize this descriptor publicly. */
struct NativeDescription {
	std::vector<TileIndex> points;
	std::vector<VehicleID> vehicles;
	std::vector<StationID> stations;
	std::vector<DepotID> depots;
	std::vector<CompanyID> companies;
	std::vector<GroupID> groups;
	std::vector<GroupID> reference_groups; ///< Read or unlink only; never expand descendants.
	std::vector<TownID> towns;
	std::vector<SignID> signs;
	std::vector<ObjectID> objects;
	std::vector<EngineID> engines;
	std::vector<StationID> reuse_candidates;
	std::optional<Order> order;
	std::optional<Json> implicit_wagon_move; ///< Ordinary invalid-destination selection, including null.
	bool group_move_shared = false;
	bool descendant_groups = false;
	NativeTileSet footprint;
	NativeTileSet terrain_height_reads; ///< Read-only corner storage; not clearing/entity targets.
	std::optional<Json> industry_platform_dependencies;

	void ReferenceGroup(GroupID id)
	{
		if (id != GroupID::Invalid() && id != ALL_GROUP && id != DEFAULT_GROUP) this->reference_groups.push_back(id);
	}

	void ReferenceGroupAncestors(GroupID id)
	{
		std::set<GroupID> seen;
		while (id != GroupID::Invalid() && id != ALL_GROUP && id != DEFAULT_GROUP) {
			if (!seen.insert(id).second) throw InvalidParameters("Native group hierarchy is cyclic");
			const Group *group = Group::GetIfValid(id);
			if (group == nullptr || group->owner != _current_company) throw InvalidParameters("Native group ancestor is invalid");
			this->ReferenceGroup(id);
			id = group->parent;
		}
	}

	void Add(TileIndex tile)
	{
		if (tile >= Map::Size()) throw InvalidParameters("Native intent contains an invalid tile");
		this->footprint.insert(tile);
	}

	void Area(TileArea area, bool perimeter = false)
	{
		if (area.w == 0 || area.h == 0 || area.w > Map::SizeX() || area.h > Map::SizeY() ||
			TileX(area.tile) + area.w > Map::SizeX() || TileY(area.tile) + area.h > Map::SizeY()) throw InvalidParameters("Native intent footprint is invalid or oversized");
		if (perimeter) area.Expand(1);
		for (auto tile : area) this->Add(tile);
	}

	void Rectangle(TileIndex first, TileIndex last)
	{
		if (first >= Map::Size() || last >= Map::Size()) throw InvalidParameters("Native intent endpoint is invalid");
		this->Area(TileArea(TileXY(std::min(TileX(first), TileX(last)), std::min(TileY(first), TileY(last))),
			std::abs(static_cast<int>(TileX(first)) - static_cast<int>(TileX(last))) + 1, std::abs(static_cast<int>(TileY(first)) - static_cast<int>(TileY(last))) + 1));
	}

	void EntityAt(TileIndex tile)
	{
		if (IsTileType(tile, TileType::Station)) this->stations.push_back(GetStationIndex(tile));
		else if (IsDepotTile(tile)) this->depots.push_back(GetDepotIndex(tile));
		else if (IsTileType(tile, TileType::Object)) this->objects.push_back(Object::GetByTile(tile)->index);
	}

	void ClearEffects(TileIndex tile)
	{
		this->Add(tile);
		if (IsTileType(tile, TileType::TunnelBridge)) this->Rectangle(tile, GetOtherTunnelBridgeEnd(tile));
		if (IsShipDepotTile(tile)) this->Add(GetOtherShipDepotTile(tile));
		if (IsTileType(tile, TileType::Water) && IsLock(tile)) {
			auto direction = GetLockDirection(tile);
			auto center = tile;
			if (GetLockPart(tile) == LockPart::Lower) center += TileOffsByDiagDir(direction);
			if (GetLockPart(tile) == LockPart::Upper) center -= TileOffsByDiagDir(direction);
			this->Add(center); this->Add(center + TileOffsByDiagDir(direction)); this->Add(center - TileOffsByDiagDir(direction));
		}
		if (IsTileType(tile, TileType::Object)) this->Area(Object::GetByTile(tile)->location);
		if (IsTileType(tile, TileType::House)) {
			/* Native houses occupy at most 2x2; include all possible north parts. */
			TileArea around(tile, 1, 1); around.Expand(1); this->Area(around);
		}
		if (IsTileType(tile, TileType::Station)) {
			const BaseStation *base = BaseStation::Get(GetStationIndex(tile));
			if (IsAirportTile(tile)) {
				const Station *station = Station::Get(base->index);
				for (auto affected : station->airport) this->Add(affected);
			} else if (IsRailStationTile(tile)) {
				this->Area(Station::Get(base->index)->train_station);
			} else if (IsDockTile(tile)) {
				/* Land/water parts are adjacent; no raw dock direction read on water. */
				TileArea around(tile, 1, 1); around.Expand(1);
				for (auto affected : around) if (IsDockTile(affected) && GetStationIndex(affected) == base->index) this->Add(affected);
			}
		}
	}
};

template <typename T>
void NativeArgument(NativeDescription &intent, const T &argument)
{
	if constexpr (std::is_same_v<T, TileIndex>) {
		if (argument >= Map::Size()) throw InvalidParameters("Native tile argument is invalid");
		intent.points.push_back(argument);
	} else if constexpr (std::is_same_v<T, VehicleID>) {
		if (argument != VehicleID::Invalid()) intent.vehicles.push_back(argument);
	} else if constexpr (std::is_same_v<T, StationID>) {
		if (argument != StationID::Invalid() && argument != NEW_STATION && argument != ADJACENT_STATION) intent.stations.push_back(argument);
	} else if constexpr (std::is_same_v<T, DepotID>) {
		if (argument != DepotID::Invalid()) intent.depots.push_back(argument);
	} else if constexpr (std::is_same_v<T, CompanyID>) {
		intent.companies.push_back(argument);
	} else if constexpr (std::is_same_v<T, GroupID>) {
		if (argument == NEW_GROUP) throw InvalidParameters("Create a group before assigning vehicles");
		if (argument != GroupID::Invalid() && argument != ALL_GROUP && argument != DEFAULT_GROUP) intent.groups.push_back(argument);
	} else if constexpr (std::is_same_v<T, TownID>) {
		/* Ordinary BuildRoad ignores its required Invalid town argument. */
		if (argument != TownID::Invalid()) intent.towns.push_back(argument);
	} else if constexpr (std::is_same_v<T, SignID>) {
		intent.signs.push_back(argument);
	} else if constexpr (std::is_same_v<T, EngineID>) {
		if (argument != EngineID::Invalid()) intent.engines.push_back(argument);
	} else if constexpr (std::is_same_v<T, Order>) {
		if (argument.GetType() != OT_GOTO_STATION && argument.GetType() != OT_GOTO_DEPOT && argument.GetType() != OT_GOTO_WAYPOINT && argument.GetType() != OT_CONDITIONAL) throw InvalidParameters("This native order variant is not supported");
		intent.order = argument;
	} else if constexpr (std::is_same_v<T, Axis>) {
		if (argument != Axis::X && argument != Axis::Y) throw InvalidParameters("Invalid native axis");
	} else if constexpr (std::is_same_v<T, DiagDirection>) {
		if (argument >= DiagDirection::End) throw InvalidParameters("Invalid native direction");
	} else if constexpr (std::is_same_v<T, Track>) {
		if (argument >= Track::End) throw InvalidParameters("Invalid native track");
	} else if constexpr (std::is_same_v<T, TransportType>) {
		if (argument != TransportType::Rail && argument != TransportType::Road && argument != TransportType::Water) throw InvalidParameters("Invalid native transport");
	} else if constexpr (std::is_same_v<T, SignalType>) {
		if (argument >= SignalType::End) throw InvalidParameters("Invalid native signal type");
	} else if constexpr (std::is_same_v<T, SignalVariant>) {
		if (argument >= SignalVariant::End) throw InvalidParameters("Invalid native signal variant");
	} else if constexpr (std::is_same_v<T, DepotCommandFlags>) {
		if (argument.Test(DepotCommandFlag::MassSend)) throw InvalidParameters("Native mass depot actions are unsupported");
	}
}

/** Share native geometry; keep read-only height corners separate from clear effects. */
void NativeTerraformFootprint(NativeDescription &intent, TileIndex tile, Slope corners, bool raise)
{
	if ((corners & ~SLOPE_ELEVATED) != 0 || corners == SLOPE_FLAT) throw InvalidParameters("Invalid native terraform corners");
	TerraformGeometry geometry;
	auto [cost, error_tile] = CollectTerraformLandGeometry(tile, corners, raise, geometry);
	if (cost.Failed()) throw InvalidParameters("Native terraform geometry is invalid");
	for (TileIndex dirty : geometry.dirty_tiles) {
		/* Only actual dirty tiles can trigger native tile clearing/linked effects. */
		intent.ClearEffects(dirty);
		/* Surface validation reads all four corners, including unchanged storage.
		 * Recursion's orthogonal neighbor reads are included in this same union.
		 * Keep in-map Void corners: native freeform-edge terraforming changes them. */
		for (auto offset : {TileIndexDiffC{0, 0}, TileIndexDiffC{1, 0}, TileIndexDiffC{0, 1}, TileIndexDiffC{1, 1}}) {
			TileIndex corner = AddTileIndexDiffCWrap(dirty, offset);
			if (corner != INVALID_TILE) intent.terrain_height_reads.insert(corner);
		}
	}
}

template <Commands Tcommand>
NativeDescription DescribeNative(const CommandDataBuffer &data)
{
	if (data.size() > NATIVE_MAX_COMMAND_BYTES) throw InvalidParameters("Native command data is oversized");
	using Arguments = typename CommandTraits<Tcommand>::Args;
	auto arguments = EndianBufferReader::ToValue<Arguments>(data);
	/* Reader defaults truncated values; an exact roundtrip rejects truncation and trailing bytes. */
	if (EndianBufferWriter<CommandDataBuffer>::FromValue(arguments) != data) throw InvalidParameters("Native command data is malformed or noncanonical");
	if constexpr (Tcommand == Commands::BuildObjectArea || Tcommand == Commands::PlaceHouseArea) {
		/* Genuine one-point picker clicks use these commands. Retain their
		 * native quota/test/callback semantics without admitting a bulk action. */
		if (std::get<0>(arguments) != std::get<1>(arguments)) throw InvalidParameters("Native area construction requires a single selected tile");
	}
	NativeDescription intent;
	if constexpr (Tcommand == Commands::StoryPageButton || Tcommand == Commands::GoalQuestionAnswer) {
		/* Story helpers validate dummy tile / selected targets in their own normal context. */
	} else if constexpr (Tcommand == Commands::CloneOrder) {
		/* Native CO_UNSHARE deliberately ignores veh_src, including invalid IDs. */
		NativeArgument(intent, std::get<1>(arguments));
		if (std::get<0>(arguments) != CO_UNSHARE) NativeArgument(intent, std::get<2>(arguments));
	} else if constexpr (Tcommand == Commands::AlterGroup) {
		/* Rename ignores parent; SetParent reads its ancestry, not its descendants. */
		NativeArgument(intent, std::get<1>(arguments));
		if (std::get<0>(arguments) == AlterGroupMode::SetParent) intent.ReferenceGroupAncestors(std::get<2>(arguments));
	} else {
		std::apply([&](const auto &... argument) { (NativeArgument(intent, argument), ...); }, arguments);
	}
	if constexpr (Tcommand == Commands::MoveRailVehicle) {
		if (std::get<1>(arguments) == VehicleID::Invalid()) {
			const Train *source = Train::GetIfValid(std::get<0>(arguments));
			if (source == nullptr || source->owner != _current_company || !IsRailDepotTile(source->tile)) throw InvalidParameters("Implicit wagon move requires an owned train in a rail depot");
			const Depot *depot = Depot::GetByTile(source->tile);
			/* Native ordinary selection bypasses engines. Autoreplace is not an
			 * admitted public command flag; the native execution bypass is unchanged. */
			VehicleID selected = source->IsEngine() ? VehicleID::Invalid() : FindRailVehicleMoveDestination(source);
			intent.implicit_wagon_move = Json{{"source_tile", source->tile.base()}, {"source_is_engine", source->IsEngine()},
				{"source_depot_id", depot->index.base()}, {"selected_vehicle_id", selected == VehicleID::Invalid() ? Json(nullptr) : Json(selected.base())}};
			intent.Add(source->tile);
			intent.depots.push_back(depot->index);
			if (selected != VehicleID::Invalid()) intent.vehicles.push_back(selected);
		}
	}
	if constexpr (Tcommand == Commands::AddVehicleToGroup) intent.group_move_shared = std::get<2>(arguments);
	if constexpr (Tcommand == Commands::DeleteGroup || Tcommand == Commands::SetGroupLivery) intent.descendant_groups = true;
	if constexpr (Tcommand == Commands::SetGroupFlag) intent.descendant_groups = std::get<3>(arguments);
	if constexpr (Tcommand == Commands::AlterGroup) intent.descendant_groups = std::get<0>(arguments) == AlterGroupMode::SetParent;
	if constexpr (Tcommand == Commands::DeleteGroup || Tcommand == Commands::AlterGroup || Tcommand == Commands::SetGroupLivery) {
		GroupID selected;
		if constexpr (Tcommand == Commands::AlterGroup) selected = std::get<1>(arguments);
		else selected = std::get<0>(arguments);
		const Group *group = Group::GetIfValid(selected);
		bool parent_reference = Tcommand == Commands::DeleteGroup;
		if constexpr (Tcommand == Commands::AlterGroup) parent_reference = std::get<0>(arguments) == AlterGroupMode::SetParent;
		if constexpr (Tcommand == Commands::SetGroupLivery) parent_reference = std::get<2>(arguments) == Colours::Invalid;
		if (group != nullptr && parent_reference) intent.ReferenceGroup(group->parent);
	}
	if constexpr (Tcommand == Commands::CloneVehicle || Tcommand == Commands::AutoreplaceVehicle || Tcommand == Commands::AddVehicleToGroup) {
		VehicleID selected;
		if constexpr (Tcommand == Commands::AutoreplaceVehicle) selected = std::get<0>(arguments);
		else selected = std::get<1>(arguments);
		const Vehicle *vehicle = Vehicle::GetIfValid(selected);
		if (vehicle == nullptr || vehicle->owner != _current_company) throw InvalidParameters("Native vehicle target is invalid");
		std::vector<const Vehicle *> affected{vehicle};
		if constexpr (Tcommand == Commands::AddVehicleToGroup) {
			if (intent.group_move_shared) {
				affected.clear();
				affected = NativeSharedVehicles(vehicle);
			}
		}
		if constexpr (Tcommand == Commands::CloneVehicle) intent.ReferenceGroup(vehicle->group_id);
		for (const Vehicle *head : affected) {
			uint count = 0;
			for (const Vehicle *part : NativeConsistParts(head)) {
				if (++count > NATIVE_MAX_CONSIST_PARTS) throw InvalidParameters("Native consist exceeds the native train-length bound");
				if constexpr (Tcommand == Commands::AddVehicleToGroup) intent.ReferenceGroup(part->group_id);
				else intent.engines.push_back(part->engine_type);
				if constexpr (Tcommand == Commands::AutoreplaceVehicle) {
					intent.ReferenceGroupAncestors(part->group_id);
					EngineID replacement = EngineReplacementForCompany(Company::Get(_current_company), part->engine_type, part->group_id);
					if (replacement != EngineID::Invalid()) intent.engines.push_back(replacement);
				}
			}
		}
	}

	if (intent.order.has_value()) {
		const Order &order = *intent.order;
		if (order.IsType(OT_GOTO_STATION) || order.IsType(OT_GOTO_WAYPOINT)) intent.stations.push_back(order.GetDestination().ToStationID());
		else if (order.IsType(OT_GOTO_DEPOT) && !order.GetDepotActionType().Test(OrderDepotActionFlag::NearestDepot)) {
			if (intent.vehicles.empty()) throw InvalidParameters("Native depot order has no vehicle");
			const Vehicle *vehicle = Vehicle::GetIfValid(intent.vehicles.front());
			if (vehicle == nullptr) throw InvalidParameters("Native vehicle target is invalid");
			if (vehicle->type == VehicleType::Aircraft) intent.stations.push_back(order.GetDestination().ToStationID());
			else intent.depots.push_back(order.GetDestination().ToDepotID());
		}
	}

	for (auto tile : intent.points) intent.Add(tile);
	if (intent.points.size() == 2) intent.Rectangle(intent.points[0], intent.points[1]);
	if (auto construction = ConstructionIntentTiles(Tcommand, data)) {
		/* Prospecting chooses no source tile: its native zero is a placeholder. */
		if constexpr (Tcommand == Commands::BuildIndustry) intent.footprint.clear();
		for (TileIndex tile : *construction) intent.Add(tile);
	}
	if constexpr (Tcommand == Commands::BuildIndustry) {
		if (auto dependencies = CaptureIndustryPlatformDependencies(data)) {
			intent.industry_platform_dependencies = std::move(dependencies->proof);
			intent.companies.insert(intent.companies.end(), dependencies->infrastructure_owners.begin(), dependencies->infrastructure_owners.end());
		}
	}
	if constexpr (Tcommand == Commands::BuildRailStation) {
		auto width = std::get<3>(arguments), height = std::get<4>(arguments);
		if (std::get<2>(arguments) == Axis::X) std::swap(width, height);
		intent.Area(TileArea(std::get<0>(arguments), width, height), !std::get<8>(arguments));
	} else if constexpr (Tcommand == Commands::BuildRoadStop) {
		intent.Area(TileArea(std::get<0>(arguments), std::get<1>(arguments), std::get<2>(arguments)), !std::get<10>(arguments));
	} else if constexpr (Tcommand == Commands::RemoveRoadStop) {
		intent.Area(TileArea(std::get<0>(arguments), std::get<1>(arguments), std::get<2>(arguments)));
	} else if constexpr (Tcommand == Commands::BuildRailWaypoint || Tcommand == Commands::BuildRoadWaypoint) {
		intent.Area(TileArea(std::get<0>(arguments), std::get<2>(arguments), std::get<3>(arguments)), !std::get<7>(arguments));
	} else if constexpr (Tcommand == Commands::BuildAirport) {
		auto type = std::get<1>(arguments), layout = std::get<2>(arguments);
		if (type >= NUM_AIRPORTS) throw InvalidParameters("Native airport type is invalid");
		const AirportSpec *airport = AirportSpec::Get(type);
		if (layout >= airport->layouts.size() || !airport->IsWithinMapBounds(layout, std::get<0>(arguments))) throw InvalidParameters("Native airport layout is invalid");
		uint width = airport->size_x, height = airport->size_y;
		if (airport->layouts[layout].rotation == Direction::E || airport->layouts[layout].rotation == Direction::W) std::swap(width, height);
		intent.Area(TileArea(std::get<0>(arguments), width, height), !std::get<4>(arguments));
		/* Native clearance checks may lie outside the airport's regular dimensions. */
		for (const auto &entry : airport->layouts[layout].tiles) intent.Add(std::get<0>(arguments) + ToTileIndexDiff(entry.ti));
	} else if constexpr (Tcommand == Commands::BuildShipDepot) {
		intent.Add(std::get<0>(arguments) + TileOffsByAxis(std::get<1>(arguments)));
	} else if constexpr (Tcommand == Commands::BuildLock || Tcommand == Commands::BuildDock) {
		auto tile = std::get<0>(arguments);
		auto direction = GetInclinedSlopeDirection(GetTileSlope(tile));
		if (direction == DiagDirection::Invalid) throw InvalidParameters("Native water construction has no valid slope");
		if constexpr (Tcommand == Commands::BuildLock) {
			intent.Add(tile + TileOffsByDiagDir(direction)); intent.Add(tile - TileOffsByDiagDir(direction));
		} else {
			auto water = tile + TileOffsByDiagDir(ReverseDiagDir(direction));
			intent.Rectangle(tile, water);
			if (!std::get<2>(arguments)) {
				TileArea area(TileXY(std::min(TileX(tile), TileX(water)), std::min(TileY(tile), TileY(water))),
					std::abs(static_cast<int>(TileX(tile)) - static_cast<int>(TileX(water))) + 1, std::abs(static_cast<int>(TileY(tile)) - static_cast<int>(TileY(water))) + 1);
				intent.Area(area, true);
			}
		}
	} else if constexpr (Tcommand == Commands::TerraformLand) {
		NativeTerraformFootprint(intent, std::get<0>(arguments), std::get<1>(arguments), std::get<2>(arguments));
	} else if constexpr (Tcommand == Commands::LevelLand) {
		/* One-point native Raise/Lower is exactly one N-corner TerraformLand.
		 * Retain the original LevelLand command, callback, cost and quota handling. */
		auto mode = std::get<3>(arguments);
		if (std::get<0>(arguments) != std::get<1>(arguments) || (mode != LevelMode::Raise && mode != LevelMode::Lower)) {
			throw InvalidParameters("Native bulk leveling is not supported by visual intent capture");
		}
		NativeTerraformFootprint(intent, std::get<0>(arguments), SLOPE_N, mode == LevelMode::Raise);
	} else if constexpr (Tcommand == Commands::BuildTunnel) {
		auto start = std::get<0>(arguments), end = start;
		auto [slope, height] = GetTileSlopeZ(start);
		auto direction = GetInclinedSlopeDirection(slope);
		if (direction == DiagDirection::Invalid) throw InvalidParameters("Native tunnel has no valid entrance slope");
		for (;;) {
			end += TileOffsByDiagDir(direction); intent.Add(end);
			if (!IsValidTile(end)) throw InvalidParameters("Native tunnel crosses the map border");
			auto [end_slope, end_height] = GetTileSlopeZ(end);
			if (end_height == height) {
				if (end_slope != ComplementSlope(slope)) NativeTerraformFootprint(intent, end, end_slope & slope, false);
				break;
			}
		}
	}
	if constexpr (Tcommand == Commands::BuildSignalLong) {
		if (std::get<6>(arguments)) throw InvalidParameters("Native signal autofill is unsupported");
	} else if constexpr (Tcommand == Commands::RemoveSignalLong) {
		if (std::get<3>(arguments)) throw InvalidParameters("Native signal autofill is unsupported");
	}
	if constexpr (Tcommand == Commands::ClearArea || Tcommand == Commands::BuildCanal || Tcommand == Commands::ConvertRoad || Tcommand == Commands::ConvertRail || Tcommand == Commands::PlantTree) {
		constexpr size_t diagonal_argument = Tcommand == Commands::ClearArea ? 2 : 3;
		for (auto tile : CreateOrthoDiagonalArea(std::get<0>(arguments), std::get<1>(arguments), std::get<diagonal_argument>(arguments))) intent.Add(tile);
	}
	if constexpr (Tcommand == Commands::LandscapeClear || Tcommand == Commands::ClearArea || Tcommand == Commands::BuildBridge || Tcommand == Commands::BuildObject || Tcommand == Commands::BuildObjectArea || Tcommand == Commands::PlaceHouse || Tcommand == Commands::PlaceHouseArea || Tcommand == Commands::BuildRailStation || Tcommand == Commands::BuildRoadStop || Tcommand == Commands::BuildAirport) {
		auto initial = intent.footprint;
		for (auto tile : initial) intent.ClearEffects(tile);
	}
	for (auto tile : intent.footprint) intent.EntityAt(tile);
	for (auto tile : intent.terrain_height_reads) intent.Add(tile);
	if constexpr (Tcommand == Commands::BuildRailStation || Tcommand == Commands::BuildRoadStop || Tcommand == Commands::BuildAirport || Tcommand == Commands::BuildDock || Tcommand == Commands::BuildRailWaypoint || Tcommand == Commands::BuildRoadWaypoint || Tcommand == Commands::BuildBuoy) {
		/* Pin all possible normal deleted-sign reuse candidates near this intent. */
		if (BaseStation::GetPoolSize() > 64000) throw InvalidParameters("Station pool exceeds the native inspection bound");
		auto origin = std::get<0>(arguments);
		if constexpr (Tcommand == Commands::BuildRailWaypoint || Tcommand == Commands::BuildRoadWaypoint) {
			/* Native waypoint reuse searches from the line's center, unlike station reuse. */
			auto axis = std::get<1>(arguments);
			auto count = axis == Axis::X ? std::get<3>(arguments) : std::get<2>(arguments);
			origin += (count / 2) * TileOffsByAxis(OtherAxis(axis));
		}
		for (const BaseStation *base : BaseStation::Iterate()) {
			if (base->IsInUse()) continue;
			uint distance = std::abs(static_cast<int>(TileX(origin)) - static_cast<int>(TileX(base->xy))) + std::abs(static_cast<int>(TileY(origin)) - static_cast<int>(TileY(base->xy)));
			if (distance >= 8) continue;
			if constexpr (Tcommand == Commands::BuildBuoy) {
				if (!Waypoint::IsExpected(base) || base->string_id != STR_SV_STNAME_BUOY || base->owner != OWNER_NONE || HasBit(Waypoint::From(base)->waypoint_flags, WPF_ROAD)) continue;
			} else {
				if (base->owner != _current_company) continue;
				if constexpr (Tcommand == Commands::BuildRailWaypoint || Tcommand == Commands::BuildRoadWaypoint) {
					if (!Waypoint::IsExpected(base) || HasBit(Waypoint::From(base)->waypoint_flags, WPF_ROAD) != (Tcommand == Commands::BuildRoadWaypoint)) continue;
				} else if (!Station::IsExpected(base)) continue;
			}
			intent.stations.push_back(base->index); intent.reuse_candidates.push_back(base->index);
		}
	}
	if constexpr (Tcommand == Commands::BuildVehicle) {
		const Engine *engine = Engine::GetIfValid(std::get<1>(arguments));
		if (engine == nullptr) throw InvalidParameters("Native engine target is invalid");
		if (std::get<2>(arguments) && engine->type == VehicleType::Train) {
			/* Match native GetFreeWagonsInDepot's tile-local candidates and order. */
			for (const Vehicle *vehicle : VehiclesOnTile(std::get<0>(arguments))) {
				if (vehicle->type != VehicleType::Train || vehicle->vehstatus.Test(VehState::Crashed) || !Train::From(vehicle)->IsFreeWagon()) continue;
				intent.vehicles.push_back(vehicle->index);
			}
			std::ranges::sort(intent.vehicles);
			intent.vehicles.erase(std::unique(intent.vehicles.begin(), intent.vehicles.end()), intent.vehicles.end());
		}
	}
	auto unique = [](auto &values) {
		std::ranges::sort(values);
		values.erase(std::unique(values.begin(), values.end()), values.end());
	};
	unique(intent.vehicles); unique(intent.stations); unique(intent.depots); unique(intent.companies);
	unique(intent.groups); unique(intent.reference_groups); unique(intent.towns); unique(intent.signs);
	unique(intent.objects); unique(intent.engines); unique(intent.reuse_candidates);
	return intent;
}

const NativeCommandSpec *FindNative(Commands command)
{
	for (const auto &spec : ACTIONS) for (const auto &native : spec.native) if (native.command == command) return &native;
	return nullptr;
}

} // namespace

Json ControlReferenceSchema()
{
	auto branch = [](Json properties) {
		Json required = Json::array();
		for (auto item = properties.begin(); item != properties.end(); ++item) required.push_back(item.key());
		return Json{{"type", "object"}, {"required", std::move(required)}, {"additionalProperties", false}, {"properties", std::move(properties)}};
	};
	Json choices = Json::array();
	choices.push_back(branch({{"kind", EnumSchema({"station", "waypoint", "depot", "group", "town", "sign", "story_page", "story_element"})}, {"id", IntegerSchema(0, 63999)}}));
	choices.push_back(branch({{"kind", EnumSchema({"vehicle"})}, {"id", IntegerSchema(0, 0xFEFFF)}}));
	choices.push_back(branch({{"kind", EnumSchema({"company"})}, {"id", IntegerSchema(0, MAX_COMPANIES - 1)}}));
	choices.push_back(branch({{"kind", EnumSchema({"question"})}, {"id", IntegerSchema(0, UINT16_MAX)}}));
	choices.push_back(branch({{"kind", EnumSchema({"engine"})}, {"id", IntegerSchema(0, EngineID::Invalid().base() - 1)}}));
	choices.push_back(branch({{"kind", EnumSchema({"vehicle"})}, {"vehicle_type", EnumSchema({"rail", "road", "ship", "aircraft"})}, {"unit_number", IntegerSchema(1, UINT16_MAX)}}));
	choices.push_back(branch({{"kind", EnumSchema({"station", "waypoint", "depot"})}, {"tile", FieldSchema(ACTIONS[0], "tile")}}));
	choices.push_back(branch({{"kind", EnumSchema({"engine"})}, {"vehicle_type", EnumSchema({"rail", "road", "ship", "aircraft"})},
		{"name", {{"type", "string"}, {"minLength", 1}, {"maxLength", 1024}}}}));
	return {{"anyOf", std::move(choices)}};
}

Json ResolveControlReference(const Json &parameters)
{
	RequireCompany();
	if (!MatchesSchema(ControlReferenceSchema(), parameters)) throw InvalidParameters("Invalid control reference");
	Parameters p(parameters);
	const std::string kind = p.Text("kind");
	const uint32_t id = static_cast<uint32_t>(p.Int("id", 0, UINT32_MAX, 0));
	auto unavailable = []() -> Json { throw InvalidParameters("Control reference unavailable"); };
	auto reference = [&](const auto *item) -> Json {
		if (item == nullptr) return unavailable();
		return {{"kind", kind}, {"reference", Handle(item)}};
	};
	if (kind == "story_page" || kind == "story_element" || kind == "question") return ResolveStoryControlReference(kind, id);
	if (kind == "vehicle") {
		const Vehicle *vehicle = nullptr;
		if (parameters.contains("unit_number")) {
			const std::string type = p.Text("vehicle_type");
			const uint unit = p.Int("unit_number", 1, UINT16_MAX);
			AdmitWorldTextStart();
			for (const Vehicle *candidate : Vehicle::Iterate()) {
				if (candidate->owner != _current_company || !IsCompanyBuildableVehicleType(candidate) || !candidate->IsPrimaryVehicle() ||
						candidate->unitnumber != unit || type != VehicleTypeName(candidate->type)) continue;
				if (vehicle != nullptr) return unavailable();
				vehicle = candidate;
			}
		} else {
			vehicle = Vehicle::GetIfValid(VehicleID(id));
		}
		/* Parts are ordinary owned actuator targets. Other-company primary
		 * vehicles are selectable by native story vehicle buttons, without
		 * exposing any of their attributes or order tokens. */
		if (vehicle == nullptr || !IsCompanyBuildableVehicleType(vehicle) ||
				(vehicle->owner != _current_company && !vehicle->IsPrimaryVehicle())) return unavailable();
		Json result = reference(vehicle);
		if (vehicle->owner == _current_company && vehicle->IsPrimaryVehicle()) {
			if (!parameters.contains("unit_number")) AdmitWorldTextStart();
			result["order_version"] = VehicleOrderControlVersion(vehicle);
		}
		return result;
	}
	if (kind == "engine") {
		const Engine *engine = nullptr;
		if (parameters.contains("name")) {
			const std::string type = p.Text("vehicle_type");
			const std::string name = p.Text("name", "", 1024);
			AdmitWorldTextStart();
			for (const Engine *candidate : Engine::Iterate()) {
				if (type != VehicleTypeName(candidate->type) || GetString(STR_ENGINE_NAME, candidate->index) != name) continue;
				if (engine != nullptr) return unavailable();
				engine = candidate;
			}
		} else {
			engine = Engine::GetIfValid(EngineID(id));
		}
		/* Loaded models stay addressable after buildability expires; normal
		 * build/replace/preview commands decide their own current permission. */
		if (engine == nullptr || engine->type > VehicleType::Aircraft) return unavailable();
		return {{"kind", kind}, {"engine_id", engine->index.base()}};
	}
	if (kind == "station" || kind == "waypoint") {
		StationID station_id(id);
		if (parameters.contains("tile")) {
			const TileIndex tile = p.Tile("tile");
			if (!IsTileType(tile, TileType::Station)) return unavailable();
			station_id = GetStationIndex(tile);
		}
		const BaseStation *station = kind == "station" ? static_cast<const BaseStation *>(Station::GetIfValid(station_id)) : Waypoint::GetIfValid(station_id);
		if (station == nullptr || (station->owner != _current_company && station->owner != OWNER_NONE)) return unavailable();
		return reference(station);
	}
	if (kind == "depot") {
		DepotID depot_id(id);
		if (parameters.contains("tile")) {
			const TileIndex tile = p.Tile("tile");
			if (!IsDepotTile(tile) || IsHangarTile(tile)) return unavailable();
			depot_id = GetDepotIndex(tile);
		}
		const Depot *depot = Depot::GetIfValid(depot_id);
		if (depot == nullptr || !IsValidTile(depot->xy) || !IsTileOwner(depot->xy, _current_company)) return unavailable();
		return reference(depot);
	}
	if (kind == "group") {
		const Group *group = Group::GetIfValid(GroupID(id));
		if (group == nullptr || group->owner != _current_company) return unavailable();
		return reference(group);
	}
	if (kind == "sign") {
		const Sign *sign = Sign::GetIfValid(SignID(id));
		if (sign == nullptr || !CompanyCanEditSign(sign)) return unavailable();
		return reference(sign);
	}
	if (kind == "town") return reference(Town::GetIfValid(TownID(id)));
	if (kind == "company") return reference(Company::GetIfValid(CompanyID(id)));
	return unavailable();
}

static ActionResult DispatchActionImpl(const std::string &action, const Json &parameters, ActionInvocation &invocation)
{
	try {
		RequireCompany();
		ValidateShape(ACTIONS, action, parameters);
		Parameters p(parameters);

		if (auto result = DispatchStoryAction(action, parameters, invocation)) return *result;
		if (auto result = DispatchAccessAction(action, parameters, invocation)) return *result;
		if (auto result = DispatchOrdersAction(action, parameters, invocation)) return *result;
		if (auto result = DispatchManagementAction(action, parameters, invocation)) return *result;
		if (auto result = DispatchConstructionAction(action, parameters, invocation)) return *result;
		if (auto result = DispatchCompanyAction(action, parameters, invocation)) return *result;

		if (action == "road.build" || action == "road.remove" || action == "road.convert") {
			auto start = p.Tile("start"), end = p.Tile("end");
			BoundedArea(start, end);
			auto road_type = p.Road();
			if (action == "road.convert") return invocation.Post<Commands::ConvertRoad>(end, start, road_type, false);
			if (TileX(start) != TileX(end) && TileY(start) != TileY(end)) throw InvalidParameters("Road endpoints must share x or y");
			Axis axis = TileY(start) != TileY(end) ? Axis::Y : Axis::X;
			if (action == "road.remove") return invocation.Post<Commands::RemoveRoadLong>(end, start, road_type, axis, start > end, start <= end);
			auto one_way = p.Bool("one_way") ? DisallowedRoadDirections(DisallowedRoadDirection::Northbound) : DisallowedRoadDirections{};
			return invocation.Post<Commands::BuildRoadLong>(end, start, road_type, axis, one_way, start > end, start <= end, true);
		}
		if (action == "road.stop.remove") return invocation.Post<Commands::RemoveRoadStop>(p.Tile("tile"), p.Int("width", 1, NATIVE_MAX_STATION_EXTENT, 1), p.Int("height", 1, NATIVE_MAX_STATION_EXTENT, 1), p.StopType(), p.Bool("remove_road"));

		if (action == "rail.build") return invocation.Post<Commands::BuildRail>(p.Tile("tile"), p.Rail(), p.GetTrack(), false);
		if (action == "rail.remove") return invocation.Post<Commands::RemoveRail>(p.Tile("tile"), p.GetTrack());
		if (action == "rail.build_line" || action == "rail.remove_line" || action == "rail.convert") {
			auto start = p.Tile("start"), end = p.Tile("end");
			BoundedArea(start, end);
			if (action == "rail.convert") return invocation.Post<Commands::ConvertRail>(end, start, p.Rail(), false);
			if (action == "rail.remove_line") return invocation.Post<Commands::RemoveRailLong>(end, start, p.GetTrack());
			return invocation.Post<Commands::BuildRailLong>(end, start, p.Rail(), p.GetTrack(), false, true);
		}
		if (action == "rail.station.remove" || action == "rail.waypoint.remove") {
			auto start = p.Tile("start"), end = p.Tile("end");
			BoundedArea(start, end);
			if (action == "rail.station.remove") return invocation.Post<Commands::RemoveFromRailStation>(start, end, p.Bool("keep_rail", true));
			return invocation.Post<Commands::RemoveFromRailWaypoint>(start, end, p.Bool("keep_rail", true));
		}
		if (action == "rail.signal.build") {
			static const std::array<std::string_view, 6> names = {"block", "entry", "exit", "combo", "path", "oneway_path"};
			auto name = p.Text("signal_type", "path");
			auto type = std::ranges::find(names, name);
			if (type == names.end()) throw InvalidParameters("Invalid signal_type");
			return invocation.Post<Commands::BuildSignal>(p.Tile("tile"), p.GetTrack(), static_cast<SignalType>(type - names.begin()), p.Bool("semaphore") ? SignalVariant::Semaphore : SignalVariant::Electric, p.Bool("convert"), false, false, SignalType::Block, SignalType::Block, p.Int("direction_cycles", 0, 3, 0), 0);
		}
		if (action == "rail.signal.remove") return invocation.Post<Commands::RemoveSignal>(p.Tile("tile"), p.GetTrack());
		if (action == "water.depot.build") return invocation.Post<Commands::BuildShipDepot>(p.Tile("tile"), p.GetAxis());
		if (action == "water.dock.build") return invocation.Post<Commands::BuildDock>(p.Tile("tile"), p.Station("station_id", true), p.Bool("adjacent", true));
		if (action == "water.buoy.build") return invocation.Post<Commands::BuildBuoy>(p.Tile("tile"));
		if (action == "water.lock.build") return invocation.Post<Commands::BuildLock>(p.Tile("tile"));
		if (action == "water.canal.build") {
			auto start = p.Tile("start"), end = p.Tile("end");
			BoundedArea(start, end, 1024);
			return invocation.Post<Commands::BuildCanal>(end, start, WaterClass::Canal, false);
		}
		if (action == "airport.build") return invocation.Post<Commands::BuildAirport>(p.Tile("tile"), p.Int("airport_type", 0, NUM_AIRPORTS - 1), p.Int("layout", 0, 255, 0), p.Station("station_id", true), p.Bool("adjacent", true));
		if (action == "airport.open_close") return invocation.Post<Commands::OpenCloseAirport>(p.Station());
		if (action == "station.rename") return invocation.Post<Commands::RenameStation>(p.Station(), p.Text("name"));

		if (action == "vehicle.build") {
			auto engine_id = EngineID(p.Int("engine_id", 0, EngineID::Invalid().base() - 1));
			const Engine *engine = Engine::GetIfValid(engine_id);
			if (engine == nullptr || !IsEngineBuildable(engine_id, engine->type, _current_company)) throw InvalidParameters("Engine is unavailable");
			CargoType cargo = static_cast<CargoType>(p.Int("cargo_id", 0, NUM_CARGO - 1, INVALID_CARGO));
			if (cargo != INVALID_CARGO && !CargoSpec::Get(cargo)->IsValid()) throw InvalidParameters("Invalid cargo_id");
			return invocation.Post<Commands::BuildVehicle>(p.Tile("tile"), engine_id, true, cargo, ClientID::Invalid);
		}
		if (action == "vehicle.clone") return invocation.Post<Commands::CloneVehicle>(p.Tile("tile"), p.Vehicle(), p.Bool("share_orders"));
		if (action == "vehicle.start_stop") return invocation.Post<Commands::StartStopVehicle>(TileIndex{0}, p.Vehicle(), true);
		if (action == "vehicle.sell") return invocation.Post<Commands::SellVehicle>(TileIndex{0}, p.Vehicle(), p.Bool("sell_chain", true), false, ClientID::Invalid);
		if (action == "vehicle.refit") {
			auto cargo = static_cast<CargoType>(p.Int("cargo_id", 0, NUM_CARGO - 1));
			if (!CargoSpec::Get(cargo)->IsValid()) throw InvalidParameters("Invalid cargo_id");
			auto subtype = parameters.contains("subtype") && parameters.at("subtype") == "keep" ? 255 : p.Int("subtype", 0, 254, 0);
			return invocation.Post<Commands::RefitVehicle>(TileIndex{0}, p.Vehicle(), cargo, subtype, false, false, p.Int("count", 1, 255, 0));
		}
		if (action == "vehicle.depot") return invocation.Post<Commands::SendVehicleToDepot>(p.Vehicle(), p.Bool("service") ? DepotCommandFlags(DepotCommandFlag::Service) : DepotCommandFlags{}, VehicleListIdentifier{});
		if (action == "vehicle.rename") return invocation.Post<Commands::RenameVehicle>(p.Vehicle(), p.Text("name"));
		if (action == "vehicle.reverse") {
			auto id = p.Vehicle();
			if (Vehicle::Get(id)->type == VehicleType::Train) return invocation.Post<Commands::ReverseTrainDirection>(TileIndex{0}, id, false);
			if (Vehicle::Get(id)->type == VehicleType::Road) return invocation.Post<Commands::TurnRoadVehicle>(TileIndex{0}, id);
			throw InvalidParameters("Only road and rail vehicles can reverse");
		}
		if (action == "train.wagon.move") return invocation.Post<Commands::MoveRailVehicle>(TileIndex{0}, p.Vehicle(), parameters.contains("destination_vehicle_id") ? p.Vehicle("destination_vehicle_id") : VehicleID::Invalid(), p.Bool("move_chain"));

		if (action == "terrain.raise" || action == "terrain.lower") return invocation.Post<Commands::TerraformLand>(p.Tile("tile"), static_cast<Slope>(p.Int("corners", 1, 15, 15)), action == "terrain.raise");
		if (action == "terrain.level") {
			auto start = p.Tile("start"), end = p.Tile("end");
			BoundedArea(start, end, 1024);
			return invocation.Post<Commands::LevelLand>(end, start, false, LevelMode::Level);
		}
		if (action == "terrain.clear") return invocation.Post<Commands::LandscapeClear>(p.Tile("tile"));
		if (action == "bridge.build") {
			auto start = p.Tile("start"), end = p.Tile("end");
			BoundedArea(start, end);
			return invocation.Post<Commands::BuildBridge>(end, start, p.Transport(true), p.Int("bridge_type", 0, MAX_BRIDGES - 1), p.Rail(), p.Road());
		}
		if (action == "tunnel.build") return invocation.Post<Commands::BuildTunnel>(p.Tile("tile"), p.Transport(false), p.Rail(), p.Road());
		if (action == "company.rename") return invocation.Post<Commands::RenameCompany>(p.Text("name"));
		if (action == "company.give_money") return invocation.Post<Commands::GiveMoney>(p.Int("amount", 1, INT32_MAX), CompanyID(p.Int("company_id", 0, MAX_COMPANIES - 1)));
		return {false, "Action has no dispatcher", "unsupported_action"};
	} catch (const InvalidParameters &error) {
		return {false, error.what(), "invalid_parameters"};
	} catch (const Json::exception &) {
		return {false, "Invalid JSON parameter type or range", "invalid_parameters"};
	}
}

ActionResult DispatchAction(const std::string &action, const Json &parameters)
{
	ActionInvocation invocation(ActionInvocation::Mode::Submit);
	return DispatchActionImpl(action, parameters, invocation);
}

Json EstimateAction(const std::string &action, const Json &parameters)
{
	struct RestoreSeeds {
		SavedRandomSeeds seeds;
		RestoreSeeds() { SaveRandomSeeds(&this->seeds); }
		~RestoreSeeds() { RestoreRandomSeeds(this->seeds); }
	} restore_seeds;
	auto unavailable = [](const char *code) { return Json{{"error", {{"code", code}}}}; };
	try {
		RequireCompany();
		if (action == "ui.apply") return unavailable("estimate_unavailable");
		ValidateShape(ACTIONS, action, parameters);
		/* An estimate can include expensive native callbacks and target proofs.
		 * This separate world-wide gate does not consume text/refit admission. */
		using Clock = std::chrono::steady_clock;
		static std::optional<Clock::time_point> last_start;
		const auto now = Clock::now();
		if (last_start && now - *last_start < std::chrono::seconds(1)) return unavailable("estimate_rate_limited");
		last_start = now;
		if (!CaptureActionTargets(action, parameters).is_array()) return unavailable("invalid_targets");
		ActionInvocation invocation(ActionInvocation::Mode::Estimate);
		const ActionResult prepared = DispatchActionImpl(action, parameters, invocation);
		if ((action == "road.depot.build" || action == "rail.depot.build") && invocation.Calls() == 0 && prepared.prequeue_result &&
				(prepared.code == "depot_direction_unavailable" || prepared.code == "depot_direction_ambiguous")) {
			/* A failed auto selection has no native quote or created entity, but
			 * preserves its bounded gameplay error in the normal estimate envelope. */
			Json result = *prepared.prequeue_result;
			result["advisory"] = true;
			result["test_and_execute_may_differ"] = false; // Neither depot command has NoTest.
			return {{"estimate", std::move(result)}};
		}
		if (invocation.Calls() != 1 || !invocation.GetEstimate() || prepared.code != "estimate_only") {
			return unavailable(prepared.code == "estimate_unavailable" ? "estimate_unavailable" : "invalid_parameters");
		}
		const auto &native = *invocation.GetEstimate();
		/* No action argument: a test must not resolve an existing station/depot
		 * at the proposed tile into a seemingly-created reference. No tuple
		 * extras: build/refit capacity and entity results are not quote output. */
		const Json feedback = FormatCommandResult(native.command, native.cost, {});
		Json result = {{"success", feedback.at("success")}, {"cost", feedback.at("cost")},
			{"advisory", true}, {"test_and_execute_may_differ", native.no_test}};
		if (feedback.contains("error")) result["error"] = feedback.at("error");
		/* Escaping can exceed the wire budget even with a 1024-byte message. */
		if (result.dump().size() > 4096 && result.contains("error")) result["error"] = {{"code", "gameplay_rejected"}, {"message", "The game rejected the action"}};
		if (result.dump().size() > 4096) return unavailable("estimate_unavailable");
		return {{"estimate", std::move(result)}};
	} catch (const InvalidParameters &) {
		return unavailable("invalid_parameters");
	} catch (const Json::exception &) {
		return unavailable("invalid_parameters");
	}
}

ActionResult ValidateActionTargets(const std::string &action, const Json &parameters)
{
	try {
		RequireCompany();
		ValidateShape(ACTIONS, action, parameters);
		Parameters p(parameters);
		for (const char *key : {"vehicle_id", "source_vehicle_id", "destination_vehicle_id"}) if (parameters.contains(key) && action != "story.button.select_vehicle") p.Vehicle(key);
		if (parameters.contains("station_id") && action != "waypoint.rename" && action != "waypoint.label.move" && action != "station.label.move") p.Station();
		return {true, {}, {}};
	} catch (const InvalidParameters &error) {
		return {false, error.what(), "invalid_target"};
	} catch (const Json::exception &) {
		return {false, "Invalid target handle", "invalid_target"};
	}
}

Json CaptureActionTargets(const std::string &action, const Json &parameters)
{
	try {
		RequireCompany();
		ValidateShape(ACTIONS, action, parameters);
		Parameters p(parameters);
		Json targets = Json::array();
		std::map<std::pair<std::string, uint32_t>, Json> entities;
		const Company *actor_company = Company::Get(_current_company);
		targets.push_back({{"kind", "actor_company"}, {"id", _current_company.base()}, {"generation", actor_company->GetInstanceGeneration()}});
		if (auto lease = CaptureStoryTargets(action, parameters)) {
			targets.push_back({{"kind", "story_family"}, {"state", std::move(*lease)}});
			return targets;
		}
		if (auto lease = CaptureAccessTargets(action, parameters)) targets.push_back({{"kind", "access_family"}, {"state", std::move(*lease)}});
		if (auto lease = CaptureOrdersTargets(action, parameters)) targets.push_back({{"kind", "orders_family"}, {"state", std::move(*lease)}});
		if (auto lease = CaptureManagementTargets(action, parameters)) targets.push_back({{"kind", "management_family"}, {"state", std::move(*lease)}});
		if (auto lease = CaptureConstructionTargets(action, parameters)) targets.push_back({{"kind", "construction_family"}, {"state", std::move(*lease)}});
		if (auto lease = CaptureCompanyTargets(action, parameters)) targets.push_back({{"kind", "company_family"}, {"state", std::move(*lease)}});
		if (action == "train.wagon.move" && !parameters.contains("destination_vehicle_id")) {
			const Train *source = Train::GetIfValid(p.Vehicle());
			if (source == nullptr || !IsRailDepotTile(source->tile)) throw InvalidParameters("Implicit wagon move requires a train in a rail depot");
			const Depot *depot = Depot::GetByTile(source->tile);
			VehicleID selected = source->IsEngine() ? VehicleID::Invalid() : FindRailVehicleMoveDestination(source);
			using Args = CommandTraits<Commands::MoveRailVehicle>::Args;
			auto native_data = EndianBufferWriter<CommandDataBuffer>::FromValue(Args{source->index, VehicleID::Invalid(), p.Bool("move_chain")});
			/* Logical state is identical to visual capture. Exact process-local
			 * incarnations additionally fence reused parts without public handles. */
			crypto_blake2b_ctx hash;
			crypto_blake2b_init(&hash, 32);
			auto append = [&](const auto &value) {
				auto bytes = EndianBufferWriter<CommandDataBuffer>::FromValue(value);
				crypto_blake2b_update(&hash, bytes.data(), bytes.size());
			};
			append(std::string("OpenTTD integration implicit wagon incarnations v1"));
			std::set<VehicleID> seen;
			const Vehicle *destination = selected == VehicleID::Invalid() ? nullptr : Vehicle::Get(selected);
			for (const Vehicle *root : {static_cast<const Vehicle *>(source), destination}) {
				if (root == nullptr) continue;
				for (const Vehicle *part : NativeConsistParts(root)) if (seen.insert(part->index).second) {
					append(part->index.base()); append(part->GetInstanceGeneration());
				}
			}
			append(static_cast<uint32_t>(seen.size()));
			std::array<uint8_t, 32> digest;
			crypto_blake2b_final(&hash, digest.data());
			targets.push_back({{"kind", "implicit_wagon_move"}, {"depot", Handle(depot)},
				{"state", NativeIntentState(Commands::MoveRailVehicle, native_data)},
				{"incarnations", {{"count", seen.size()}, {"digest", FormatArrayAsHex(digest)}}}});
		}
		if (action == "company.give_money") {
			const Company *recipient = Company::GetIfValid(p.Int("company_id", 0, MAX_COMPANIES - 1));
			if (recipient == nullptr) throw InvalidParameters("Recipient company is unavailable");
			targets.push_back({{"kind", "recipient_company"}, {"id", recipient->index.base()}, {"generation", recipient->GetInstanceGeneration()}});
		}
		auto capture_entity = [&](TileIndex tile) {
			if (IsTileType(tile, TileType::Station)) {
				const BaseStation *station = BaseStation::Get(GetStationIndex(tile));
				entities[{"station", station->index.base()}] = {{"kind", "station"}, {"id", station->index.base()}, {"generation", station->GetInstanceGeneration()}};
			} else if (IsDepotTile(tile)) {
				const Depot *depot = Depot::Get(GetDepotIndex(tile));
				entities[{"depot", depot->index.base()}] = {{"kind", "depot"}, {"id", depot->index.base()}, {"generation", depot->GetInstanceGeneration()}};
			} else if (IsTileType(tile, TileType::Object)) {
				const Object *object = Object::GetByTile(tile);
				entities[{"object", object->index.base()}] = {{"kind", "object"}, {"id", object->index.base()}, {"generation", object->GetInstanceGeneration()}};
			}
		};
		auto capture_area = [&](TileArea area, bool join_perimeter) {
			if (area.w * area.h > 4096) throw InvalidParameters("Construction footprint exceeds the per-action budget");
			/* This is exactly GetStationAround's search radius in station_cmd.cpp. */
			if (join_perimeter) area.Expand(1);
			for (TileIndex tile : area) capture_entity(tile);
		};
		for (const char *key : {"tile", "start", "end", "destination"}) {
			if (!parameters.contains(key)) continue;
			auto tile = p.Tile(key);
			targets.push_back({{"kind", "tile"}, {"parameter", key}, {"tile", tile.base()}, {"type", to_underlying(GetTileType(tile))},
				{"height", TileHeight(tile)}, {"slope", to_underlying(GetTileSlope(tile))},
				{"owner", (IsTileType(tile, TileType::House) || IsTileType(tile, TileType::Industry)) ? Json(nullptr) : Json(GetTileOwner(tile).base())}});
			capture_entity(tile);
		}
		if (action == "object.build" || action == "house.build") {
			Commands command;
			CommandDataBuffer native_data;
			if (action == "object.build") {
				command = Commands::BuildObject;
				using Args = CommandTraits<Commands::BuildObject>::Args;
				native_data = EndianBufferWriter<CommandDataBuffer>::FromValue(Args{p.Tile("tile"), ObjectType(p.Int("object_type", 0, NUM_OBJECTS - 1)), static_cast<uint8_t>(p.Int("view", 0, 255, 0))});
			} else {
				command = Commands::PlaceHouse;
				using Args = CommandTraits<Commands::PlaceHouse>::Args;
				native_data = EndianBufferWriter<CommandDataBuffer>::FromValue(Args{p.Tile("tile"), HouseID(p.Int("house_type", 0, NUM_HOUSES - 1)), p.Bool("protected"), p.Bool("replace")});
			}
			/* Reuse the native family's bounded footprint, including a relocated HQ. */
			if (auto footprint = ConstructionIntentTiles(command, native_data)) for (TileIndex tile : *footprint) capture_entity(tile);
		}
		if (parameters.contains("start") && parameters.contains("end")) {
			auto start = p.Tile("start"), end = p.Tile("end");
			BoundedArea(start, end);
			for (uint y = std::min(TileY(start), TileY(end)); y <= std::max(TileY(start), TileY(end)); ++y) {
				for (uint x = std::min(TileX(start), TileX(end)); x <= std::max(TileX(start), TileX(end)); ++x) capture_entity(TileXY(x, y));
			}
		}
		if (action == "road.stop.remove" || action == "road.stop.build" || action == "rail.station.build" || action == "road.waypoint.build" || action == "rail.waypoint.build") {
			auto tile = p.Tile("tile");
			uint width = p.Int("width", 1, NATIVE_MAX_STATION_EXTENT, 1), height = p.Int("height", 1, NATIVE_MAX_STATION_EXTENT, 1);
			if (action == "road.stop.build") height = p.Int("length", 1, NATIVE_MAX_STATION_EXTENT, 1);
			if (action == "rail.station.build") { width = p.Int("tracks", 1, NATIVE_MAX_STATION_EXTENT); height = p.Int("length", 1, NATIVE_MAX_STATION_EXTENT); }
			if (action == "rail.station.build" && p.GetAxis() == Axis::X) std::swap(width, height);
			if (TileX(tile) + width > Map::MaxX() || TileY(tile) + height > Map::MaxY()) throw InvalidParameters("Construction footprint extends outside buildable map");
			capture_area(TileArea(tile, width, height), action != "road.stop.remove" && !p.Bool("adjacent", true));
		}
		if (action == "airport.build") {
			auto tile = p.Tile("tile");
			const AirportSpec *airport = AirportSpec::Get(p.Int("airport_type", 0, NUM_AIRPORTS - 1));
			auto layout = p.Int("layout", 0, 255, 0);
			if (!airport->IsAvailable() || layout >= static_cast<int64_t>(airport->layouts.size()) || !airport->IsWithinMapBounds(layout, tile)) throw InvalidParameters("Airport layout is unavailable or outside the map");
			uint width = airport->size_x, height = airport->size_y;
			auto rotation = airport->layouts[layout].rotation;
			if (rotation == Direction::E || rotation == Direction::W) std::swap(width, height);
			capture_area(TileArea(tile, width, height), !p.Bool("adjacent", true));
		}
		if (action == "water.dock.build") {
			auto tile = p.Tile("tile");
			auto slope_direction = GetInclinedSlopeDirection(GetTileSlope(tile));
			if (slope_direction != DiagDirection::Invalid) {
				auto direction = ReverseDiagDir(slope_direction);
				auto water = tile + TileOffsByDiagDir(direction);
				if (water < Map::Size() && IsValidTile(water)) {
					auto origin = TileXY(std::min(TileX(tile), TileX(water)), std::min(TileY(tile), TileY(water)));
					capture_area(TileArea(origin, DiagDirToAxis(direction) == Axis::X ? 2 : 1, DiagDirToAxis(direction) == Axis::Y ? 2 : 1), !p.Bool("adjacent", true));
				}
			}
		}
		if (action == "water.buoy.build") {
			/* CmdBuildBuoy has no explicit-new option; pin its normal reuse choice. */
			auto tile = p.Tile("tile");
			const Waypoint *candidate = nullptr;
			uint threshold = 8;
			for (const Waypoint *waypoint : Waypoint::Iterate()) {
				if (waypoint->IsInUse() || waypoint->string_id != STR_SV_STNAME_BUOY || waypoint->owner != OWNER_NONE || HasBit(waypoint->waypoint_flags, WPF_ROAD)) continue;
				uint distance = std::abs(static_cast<int>(TileX(tile)) - static_cast<int>(TileX(waypoint->xy))) + std::abs(static_cast<int>(TileY(tile)) - static_cast<int>(TileY(waypoint->xy)));
				if (distance < threshold) { candidate = waypoint; threshold = distance; }
			}
			targets.push_back({{"kind", "implicit_buoy_reuse"}, {"waypoint", candidate == nullptr ? Json(nullptr) : Handle(candidate)}});
		}
		for (const auto &[key, entity] : entities) targets.push_back(entity);
		return targets;
	} catch (const InvalidParameters &error) {
		return {{"error", {{"code", "invalid_target"}, {"message", error.what()}}}};
	} catch (const Json::exception &) {
		return {{"error", {{"code", "invalid_target"}, {"message", "Invalid target parameters"}}}};
	}
}

ActionResult ValidateActionTargets(const std::string &action, const Json &parameters, const Json &captured_targets)
{
	auto explicit_targets = ValidateActionTargets(action, parameters);
	if (!explicit_targets.accepted) return explicit_targets;
	Json current = CaptureActionTargets(action, parameters);
	if (!captured_targets.is_array() || !current.is_array() || captured_targets != current) return {false, "An action target changed while queued", "stale_target"};
	return {true, {}, {}};
}

Json QueryState(const std::string &query, const Json &parameters)
{
	try {
		RequireCompany();
		ValidateShape(QUERIES, query, parameters);
		Parameters p(parameters);
		if (auto result = QueryStoryState(query, parameters)) return *result;
		if (auto result = QueryAccessState(query, parameters)) return *result;
		if (auto result = QueryWorldDetailsState(query, parameters)) return *result;
		if (auto result = QueryStationDetailsState(query, parameters)) return *result;
		if (auto result = QueryVehicleDetailsState(query, parameters)) return *result;
		if (auto result = QueryIndustryDetailsState(query, parameters)) return *result;
		if (auto result = QueryOrdersState(query, parameters)) return *result;
		if (auto result = QueryManagementState(query, parameters)) return *result;
		if (auto result = QueryConstructionState(query, parameters)) return *result;
		if (auto result = QueryCompanyState(query, parameters)) return *result;
		if (auto result = QueryReportsState(query, parameters)) return *result;
		Json result = SnapshotBase();
		if (query == "map.region") {
			auto x = p.Int("x", 0, Map::MaxX()), y = p.Int("y", 0, Map::MaxY());
			auto width = p.Int("width", 1, 64), height = p.Int("height", 1, 64);
			if (x + width > Map::SizeX() || y + height > Map::SizeY()) throw InvalidParameters("Region extends outside the map");
			auto offset = p.Int("after_offset", -1, width * height - 1, -1) + 1;
			auto limit = p.Int("limit", 1, 100, 100);
			Json tiles = Json::array();
			size_t encoded_bytes = 0;
			for (; offset < width * height && tiles.size() < static_cast<size_t>(limit); ++offset) {
				auto row = y + offset / width, column = x + offset % width;
				auto tile = TileXY(column, row);
				Json item = TileCoordinates(tile);
				item["type"] = to_underlying(GetTileType(tile));
				item["height"] = TileHeight(tile);
				if (IsValidTile(tile)) {
					item["slope"] = to_underlying(GetTileSlope(tile));
					item["owner"] = (IsTileType(tile, TileType::House) || IsTileType(tile, TileType::Industry)) ? Json(nullptr) : Json(GetTileOwner(tile).base());
					if (IsTileType(tile, TileType::Station)) {
						item["station_id"] = Handle(BaseStation::Get(GetStationIndex(tile)));
						item["station_type"] = to_underlying(GetStationType(tile));
						item["hangar"] = IsHangarTile(tile);
						if (IsRailStationTile(tile) || IsRailWaypointTile(tile)) {
							item["rail_type"] = to_underlying(GetRailType(tile));
							item["station_track"] = to_underlying(GetRailStationTrack(tile));
						}
						if (IsDockTile(tile) && !IsDockWaterPart(tile)) item["dock_direction"] = to_underlying(GetDockDirection(tile));
					}
					if (IsTileType(tile, TileType::Industry)) item["industry_id"] = GetIndustryIndex(tile).base();
					if (IsTileType(tile, TileType::Water)) {
						item["water_depot"] = IsShipDepot(tile);
						item["lock"] = IsLock(tile);
						item["canal"] = IsCanal(tile);
						if (IsShipDepot(tile)) item["depot_axis"] = GetShipDepotAxis(tile) == Axis::X ? "x" : "y";
						if (IsLock(tile)) item["lock_direction"] = to_underlying(GetLockDirection(tile));
					}
					if (MayHaveRoad(tile)) {
						item["road_type"] = to_underlying(GetRoadTypeRoad(tile));
						item["tram_type"] = to_underlying(GetRoadTypeTram(tile));
						item["road_bits"] = GetAnyRoadBits(tile, RoadTramType::Road).base();
						item["tram_bits"] = GetAnyRoadBits(tile, RoadTramType::Tram).base();
					}
					if (IsTileType(tile, TileType::TunnelBridge)) {
						auto transport = GetTunnelBridgeTransportType(tile);
						item["transport"] = transport == TransportType::Rail ? "rail" : transport == TransportType::Road ? "road" : "water";
						item["tunnel"] = IsTunnel(tile);
						item["other_end"] = TileCoordinates(GetOtherTunnelBridgeEnd(tile));
						item["direction"] = to_underlying(GetTunnelBridgeDirection(tile));
						if (transport == TransportType::Rail) item["rail_type"] = to_underlying(GetRailType(tile));
					}
					if (IsTileType(tile, TileType::Road)) {
						item["road_type"] = to_underlying(GetRoadTypeRoad(tile));
						item["tram_type"] = to_underlying(GetRoadTypeTram(tile));
						item["depot"] = IsRoadDepotTile(tile);
						if (IsNormalRoadTile(tile)) {
							item["road_bits"] = GetRoadBits(tile, RoadTramType::Road).base();
							item["tram_bits"] = GetRoadBits(tile, RoadTramType::Tram).base();
							item["disallowed_road_directions"] = GetDisallowedRoadDirections(tile).base();
						}
						if (IsRoadDepotTile(tile)) item["depot_direction"] = to_underlying(GetRoadDepotDirection(tile));
						if (IsLevelCrossingTile(tile)) { item["rail_type"] = to_underlying(GetRailType(tile)); item["crossing_track"] = to_underlying(GetCrossingRailTrack(tile)); }
					}
					if (IsTileType(tile, TileType::Railway)) {
						item["rail_type"] = to_underlying(GetRailType(tile));
						item["depot"] = IsRailDepotTile(tile);
						if (IsRailDepotTile(tile)) item["depot_direction"] = to_underlying(GetRailDepotDirection(tile));
						if (IsPlainRailTile(tile)) {
							item["track_bits"] = GetTrackBits(tile).base();
							item["has_signals"] = HasSignals(tile);
							if (HasSignals(tile)) {
								item["present_signals"] = GetPresentSignals(tile);
								item["signal_states"] = GetSignalStates(tile);
								item["signals"] = Json::array();
								for (auto track : GetTrackBits(tile)) if (HasSignalOnTrack(tile, track)) item["signals"].push_back({{"track", to_underlying(track)}, {"signal_type", to_underlying(GetSignalType(tile, track))}, {"semaphore", GetSignalVariant(tile, track) == SignalVariant::Semaphore}});
							}
						}
					}
				}
				auto item_bytes = item.dump().size();
				if (encoded_bytes + item_bytes > 40 * 1024) break;
				encoded_bytes += item_bytes;
				tiles.push_back(std::move(item));
			}
			result["map_width"] = Map::SizeX();
			result["map_height"] = Map::SizeY();
			result["region"] = {{"x", x}, {"y", y}, {"width", width}, {"height", height}};
			result["next_after_offset"] = offset < width * height ? Json(offset - 1) : Json(nullptr);
			result["complete"] = offset == width * height;
			result["page_tick"] = result.at("tick");
			result["tiles"] = std::move(tiles);
			return result;
		}
		if (query == "company") {
			const Company *company = Company::Get(_current_company);
			result["name"] = GetString(STR_COMPANY_NAME, company->index);
			result["balance"] = GetAvailableMoney(company->index).base();
			result["loan"] = company->current_loan.base();
			result["maximum_loan"] = company->GetMaxLoan().base();
			result["loan_interval"] = LOAN_INTERVAL;
			const auto &settings = company->settings;
			result["settings"] = {{"autorenew_enabled", settings.engine_renew}, {"autorenew_months", settings.engine_renew_months},
				{"autorenew_minimum_money", settings.engine_renew_money}, {"renew_keep_length", settings.renew_keep_length},
				{"service_percent", settings.vehicle.servint_ispercent}, {"service_trains", settings.vehicle.servint_trains},
				{"service_road", settings.vehicle.servint_roadveh}, {"service_ships", settings.vehicle.servint_ships}, {"service_aircraft", settings.vehicle.servint_aircraft}};
			result["income_this_quarter"] = company->cur_economy.income.base();
			result["expenses_this_quarter"] = company->cur_economy.expenses.base();
			result["quarter_history"] = Json::array();
			for (uint i = 0; i < std::min<size_t>(company->num_valid_stat_ent, company->old_economy.size()); ++i) {
				const auto &quarter = company->old_economy[i];
				result["quarter_history"].push_back({{"quarter", i + 1}, {"income", quarter.income.base()}, {"expenses", quarter.expenses.base()}, {"company_value", quarter.company_value.base()}});
			}
			result["infrastructure"] = {{"rail", company->infrastructure.GetRailTotal()}, {"road", company->infrastructure.GetRoadTotal()}, {"tram", company->infrastructure.GetTramTotal()},
				{"signals", company->infrastructure.signal}, {"water", company->infrastructure.water}, {"station_tiles", company->infrastructure.station}, {"airports", company->infrastructure.airport}};
			return result;
		}
		if (query == "vehicles") {
			size_t part_budget = 256;
			result.update(PoolPage<Vehicle>(p, [](const Vehicle *vehicle) {
				return vehicle->owner == _current_company && IsCompanyBuildableVehicleType(vehicle) && (vehicle->IsPrimaryVehicle() || (vehicle->type == VehicleType::Train && Train::From(vehicle)->IsFreeWagon()));
			}, [&](const Vehicle *vehicle) { return VehicleSnapshot(vehicle, part_budget); }));
			return result;
		}
		if (query == "stations") {
			result.update(PoolPage<Station>(p, [](const Station *station) { return station->owner == _current_company; }, StationSnapshot));
			return result;
		}
		if (query == "catalog.engines") {
			auto vehicle_type = p.Text("vehicle_type");
			result.update(PoolPage<Engine>(p, [&](const Engine *engine) {
				return engine->IsEnabled() && IsEngineBuildable(engine->index, engine->type, _current_company) && (vehicle_type.empty() || vehicle_type == VehicleTypeName(engine->type));
			}, [](const Engine *engine) {
				Json item = {{"engine_id", engine->index.base()}, {"name", GetString(STR_ENGINE_NAME, engine->index)}, {"vehicle_type", VehicleTypeName(engine->type)},
					{"price", engine->GetCost().base()}, {"running_cost", engine->GetRunningCost().base()}, {"maximum_speed", engine->GetDisplayMaxSpeed()}, {"default_cargo_id", to_underlying(engine->GetDefaultCargoType())}};
				item["hidden_for_company"] = engine->company_hidden.Test(_current_company);
				Json capacities = Json::array();
				auto capacity = GetCapacityOfArticulatedParts(engine->index);
				for (uint id = 0; id < NUM_CARGO; ++id) if (capacity[static_cast<CargoType>(id)] != 0) capacities.push_back({{"cargo_id", id}, {"capacity", capacity[static_cast<CargoType>(id)]}});
				item["capacities"] = std::move(capacities);
				Json refittable = Json::array();
				for (auto id : GetUnionOfArticulatedRefitMasks(engine->index, true)) refittable.push_back(to_underlying(id));
				item["refittable_cargo_ids"] = std::move(refittable);
				if (engine->type == VehicleType::Aircraft) {
					uint16_t mail_capacity = 0;
					item["capacity"] = engine->GetDisplayDefaultCapacity(&mail_capacity);
					item["mail_capacity"] = mail_capacity;
					item["aircraft_subtype"] = AircraftVehInfo(engine->index)->subtype;
					item["range"] = engine->GetRange();
				}
				if (engine->type == VehicleType::Road) item["road_type"] = to_underlying(RoadVehInfo(engine->index)->roadtype);
				if (engine->type == VehicleType::Train) {
					Json railtypes = Json::array();
					for (auto type : RailVehInfo(engine->index)->railtypes) railtypes.push_back(to_underlying(type));
					item["rail_types"] = std::move(railtypes);
					item["wagon"] = RailVehInfo(engine->index)->railveh_type == RailVehicleType::Wagon;
				}
				return item;
			}));
			return result;
		}
		if (query == "catalog.airports") {
			result["items"] = Json::array();
			for (uint id = 0; id < NUM_AIRPORTS; ++id) {
				const AirportSpec *airport = AirportSpec::Get(id);
				if (!airport->enabled || !airport->IsAvailable()) continue;
				result["items"].push_back({{"airport_type", id}, {"name", GetString(airport->name)}, {"width", airport->size_x}, {"height", airport->size_y},
					{"layout_count", airport->layouts.size()}, {"coverage_radius", airport->catchment}, {"noise", airport->noise_level}});
			}
			return result;
		}
		if (query == "catalog.cargo") {
			result["items"] = Json::array();
			for (uint id = 0; id < NUM_CARGO; ++id) {
				const CargoSpec *cargo = CargoSpec::Get(id);
				if (cargo->IsValid()) result["items"].push_back({{"cargo_id", id}, {"name", GetString(cargo->name)}, {"freight", cargo->is_freight}});
			}
			return result;
		}
		if (query == "catalog.railtypes") {
			result["items"] = Json::array();
			for (auto type : Company::Get(_current_company)->avail_railtypes) result["items"].push_back({{"rail_type", to_underlying(type)}, {"name", GetString(GetRailTypeInfo(type)->strings.name)}});
			return result;
		}
		if (query == "catalog.roadtypes") {
			result["items"] = Json::array();
			for (auto type : Company::Get(_current_company)->avail_roadtypes) result["items"].push_back({{"road_type", to_underlying(type)}, {"name", GetString(GetRoadTypeInfo(type)->strings.name)}, {"tram", RoadTypeIsTram(type)}});
			return result;
		}
		if (query == "towns") {
			result.update(PoolPage<Town>(p, [](const Town *) { return true; }, [](const Town *town) {
				return Json{{"town_id", town->index.base()}, {"town", Handle(town)}, {"name", GetString(STR_TOWN_NAME, town->index)}, {"location", TileCoordinates(town->xy)}, {"population", town->cache.population}};
			}));
			return result;
		}
		return {{"error", {{"code", "unsupported_query"}, {"message", "Query has no dispatcher"}}}};
	} catch (const InvalidParameters &error) {
		return {{"error", {{"code", "invalid_parameters"}, {"message", error.what()}}}};
	} catch (const Json::exception &) {
		return {{"error", {{"code", "invalid_parameters"}, {"message", "Invalid JSON parameter type or range"}}}};
	}
}

Json CapabilityMatrix()
{
	auto describe = [](const Spec &spec, bool query) {
		Json properties = Json::object();
		for (const char *key : spec.required) properties[key] = nullptr;
		for (const char *key : spec.optional) properties[key] = nullptr;
		std::string target_kind = "company";
		if (properties.contains("vehicle_id")) target_kind = "vehicle";
		else if (properties.contains("group_id") || properties.contains("parent_group_id")) target_kind = "group";
		else if (properties.contains("town_id")) target_kind = "town";
		else if (properties.contains("sign_id")) target_kind = "sign";
		else if (properties.contains("depot_id")) target_kind = "depot";
		else if (properties.contains("station_id") && !properties.contains("tile")) target_kind = "station";
		else if (properties.contains("tile") || properties.contains("start")) target_kind = "tile";
		else if (query) target_kind = "none";
		Json fields = query ? Json::array({"tick", "company_id"}) : Json::array({"success", "cost", "error"});
		std::string_view name(spec.name);
		/* This reader selects several entity kinds; its optional vehicle selector
		 * describes only the refit-label variant, not every request. */
		if (name == "display.text") target_kind = "none";
		if (name == "vehicle.build" || name == "vehicle.clone") fields.push_back("vehicle_id");
		if (name == "group.create") fields.push_back("group_id");
		if (name == "sign.place") fields.push_back("sign_id");
		if (name == "town.found") fields.push_back("town_id");
		if (name == "road.depot.build" || name == "rail.depot.build" || name == "water.depot.build") fields.push_back("depot_id");
		if (name == "road.depot.build" || name == "rail.depot.build") fields.push_back("direction");
		if (name == "road.stop.build" || name == "road.waypoint.build" || name == "rail.station.build" || name == "rail.waypoint.build" ||
				name == "water.dock.build" || name == "water.buoy.build" || name == "airport.build") fields.push_back("station_id");
		if (name == "vehicle.build" || name == "vehicle.refit") { fields.push_back("capacity"); fields.push_back("mail_capacity"); fields.push_back("cargo_capacities"); }
		if (query) {
			if (name == "map.region") for (const char *field : {"map_width", "map_height", "region", "tiles", "next_after_offset", "complete", "page_tick"}) fields.push_back(field);
			else if (name == "cargo.payment_rates") for (const char *field : {"cargo_id", "amount", "distance_tiles", "money_unit", "time_unit", "income_scope", "samples"}) fields.push_back(field);
			else if (name == "catalog.bridges") for (const char *field : {"transports", "span_unit", "speed_unit", "items"}) fields.push_back(field);
			else if (name == "engine.purchase_info") for (const char *field : {"engine_id", "vehicle_type", "speed_unit", "performance"}) fields.push_back(field);
			else if (name == "town.detail") for (const char *field : {"town_id", "name", "name_truncated", "location", "city", "population", "house_count", "text", "text_truncated", "supplied_cargo", "growth_requirements", "growth", "airport_noise"}) fields.push_back(field);
			else if (name == "subsidies") for (const char *field : {"items", "next_after_id"}) fields.push_back(field);
			else if (name == "league.performance") for (const char *field : {"rows"}) fields.push_back(field);
			else if (name == "league.tables") for (const char *field : {"items", "next_after_id"}) fields.push_back(field);
			else if (name == "league.table.entries") for (const char *field : {"table_id", "title", "title_truncated", "header", "header_truncated", "footer", "footer_truncated", "items", "next_after_id"}) fields.push_back(field);
			else if (name == "goals") for (const char *field : {"scope", "items", "next_after_id"}) fields.push_back(field);
			else if (name == "station.details") for (const char *field : {"station", "name", "owner", "sign_tile", "town", "facilities", "spread", "catchment_radius", "airport", "cargo"}) fields.push_back(field);
			else if (name == "station.cargo.flows") for (const char *field : {"station", "cargo_id", "unit", "has_cargo_data", "distribution_mode", "rows", "next", "complete"}) fields.push_back(field);
			else if (name == "station.cargo.waiting") for (const char *field : {"station", "cargo_id", "distribution_mode", "route_details_suppressed", "destination_semantics", "available_units", "native_available_cached", "native_reserved_cached", "native_total_cached", "available_matches_native_cache", "native_total_addition_fits_u32", "aggregate_version", "live", "rows", "next_after_group", "complete"}) fields.push_back(field);
			else if (name == "station.links") for (const char *field : {"station", "cargo_id", "rows", "next_after_offset", "complete"}) fields.push_back(field);
			else if (name == "vehicle.details") for (const char *field : {"vehicle_id", "owner_company_id", "owned_by_actor", "name", "vehicle_type", "engine_id", "location", "stopped", "crashed", "in_depot", "status", "can_refit_now", "age_calendar_days", "maximum_age_calendar_days", "build_year", "reliability_percent", "engine_reliability_percent", "breakdowns_since_last_service", "running_cost", "running_cost_unit", "profit_this_period", "profit_last_period", "service_interval", "service_custom", "service_percent", "last_service_economy_date", "order_count", "manual_order_count", "current_order_position", "part_count", "consist_version", "parts", "next_after_part_position"}) fields.push_back(field);
			else if (name == "company.valuation") for (const char *field : {"company", "company_value"}) fields.push_back(field);
			else if (name == "vehicle.refit_subtypes") for (const char *field : {"vehicle_id", "part_id", "cargo_id", "refittable", "current_cargo", "keep_option", "subtypes", "next_after_subtype", "complete", "probe_cap_reached"}) fields.push_back(field);
			else if (name == "goal.questions") for (const char *field : {"items", "next_after_id", "next_after_generation"}) fields.push_back(field);
			else if (name == "news.custom") for (const char *field : {"history_scope", "history_start", "latest", "gap", "items", "next", "complete"}) fields.push_back(field);
			else if (name == "display.text") for (const char *field : {"kind", "target", "cargo_id", "direction", "slot", "vehicle_id", "subtype", "text", "content_version", "total_bytes", "next_after_byte"}) fields.push_back(field);
			else if (name == "industry.details") for (const char *field : {"industry_id", "name", "name_truncated", "industry_type", "location", "width", "height", "nearest_town", "announced_closure", "period_unit", "accepted_cargo_ids", "produced_cargo", "accepted", "produced", "omitted_presentation"}) fields.push_back(field);
			else if (name == "industry.monthly_stats") for (const char *field : {"industry_id", "cargo_id", "direction", "period_unit", "points"}) fields.push_back(field);
			else if (name == "goal.question") for (const char *field : {"question_id", "type", "buttons", "text", "text_truncated", "text_offset", "text_bytes", "next_text_offset"}) fields.push_back(field);
			else if (name == "story.elements") for (const char *field : {"page_id", "book_company_id", "sort_order", "date", "title", "title_truncated", "interactive", "page_version", "title_text", "items", "next_after_id"}) fields.push_back(field);
			else if (name == "story.element") for (const char *field : {"element_id", "sort_order", "type", "text", "text_truncated", "location", "goal_id", "completed", "vehicle_type", "page_id", "page_version", "text_offset", "text_bytes", "next_text_offset"}) fields.push_back(field);
			else if (name == "story.vehicle_candidates") for (const char *field : {"page_version", "items", "next_after_id"}) fields.push_back(field);
			else if (name == "company") for (const char *field : {"name", "balance", "loan", "maximum_loan", "loan_interval", "income_this_quarter", "expenses_this_quarter", "quarter_history", "infrastructure", "settings"}) fields.push_back(field);
			else if (name == "company.access") for (const char *field : {"join_policy", "participant", "listed", "permitted"}) fields.push_back(field);
			else if (name == "orders" || name == "timetable") {
    for (const char *field : {"vehicle_id", "order_version", "order_count", "manual_order_count", "current_order", "orders", "next_after_position", "shared_count", "shared_vehicles", "next_shared_after_id", "current_execution"}) fields.push_back(field);
    if (name == "timetable") for (const char *field : {"started", "start_tick", "lateness_ticks", "autofill", "preserve_wait_time", "complete", "duration_ticks", "speed_unit"}) fields.push_back(field);
   }
   else if (name == "group.stats") for (const char *field : {"scope", "group_id", "vehicle_count", "profit_this_year", "profit_last_year", "engine_count"}) fields.push_back(field);
   else if (name == "autoreplace") for (const char *field : {"scope", "group_id", "items", "next_after_id"}) fields.push_back(field);
   else if (name == "company.appearance") for (const char *field : {"president_name", "manager_face", "liveries"}) fields.push_back(field);
   else if (name == "catalog.manager_faces") for (const char *field : {"items", "next_after_id", "selected_style"}) fields.push_back(field);
   else if (name == "company.report") for (const char *field : {"company", "name", "president_name", "balance", "loan", "maximum_loan", "inaugurated_year", "inaugurated_calendar_year", "vehicle_counts", "infrastructure", "calendar_date", "economy_date", "current_quarter", "quarter_history", "yearly_expenses", "acquisition_offer"}) fields.push_back(field);
   else if (name == "world") for (const char *field : {"map", "calendar", "economy", "timekeeping", "rules"}) fields.push_back(field);
   else if (name == "land.info") for (const char *field : {"tile", "tile_type", "height", "slope", "description", "owners", "local_authority", "build_date", "house", "object", "station_spec", "accepted_cargo", "clear_preview"}) fields.push_back(field);
   else if (name == "station.site") for (const char *field : {"build_action", "selection", "footprint", "catchment", "accepted_cargo", "supplied_cargo", "authority", "feasibility"}) fields.push_back(field);
   else if (name == "town.authority") for (const char *field : {"town_id", "name", "location", "population", "ratings", "viewer_rating", "exclusivity", "viewer_unwanted_periods", "viewer_has_statue", "airport_noise", "actions"}) fields.push_back(field);
			else { fields.push_back("items"); if (properties.contains("limit")) fields.push_back("next_after_id"); }
		}
		return Json{{"name", spec.name}, {"parameters", ParameterSchema(spec)},
			{"modes", query ? Json::array({"text"}) : Json::array({"text", "visual"})}, {"target_kind", target_kind}, {"result_fields", std::move(fields)}};
	};
	Json actions = Json::array(), queries = Json::array();
	for (const auto &spec : ACTIONS) actions.push_back(describe(spec, false));
	for (const auto &spec : QUERIES) queries.push_back(describe(spec, true));
	return {{"version", 1}, {"actions", std::move(actions)}, {"queries", std::move(queries)},
		{"limits", {{"region_width", 64}, {"region_height", 64}, {"map_page_items", 100}, {"map_page_bytes", 40960}, {"page_items", 256}, {"page_scanned_slots", 4096}, {"vehicle_parts_per_page", 256}, {"order_page_items", 64}, {"shared_member_page_items", 64}, {"order_cohort_members", NATIVE_MAX_SHARED_VEHICLES}, {"group_page_items", 100}, {"manager_face_page_items", 16}, {"native_operation_state_bytes", 16384}, {"construction_extent", MAX_MAP_SIZE}, {"construction_area", 4096}, {"station_extent", NATIVE_MAX_STATION_EXTENT}, {"consist_parts", NATIVE_MAX_CONSIST_PARTS}, {"native_spatial_tiles", NATIVE_MAX_SPATIAL_TILES}, {"terrain_level_area", 1024}, {"display_text_chunk_bytes", 2048}, {"text_refit_interval_ms", 1000}, {"estimate_interval_ms", 1000}, {"estimate_result_bytes", 4096}, {"control_reference_scan_interval_ms", 1000}, {"valuation_interval_ms", 1000}, {"station_waiting_interval_ms", 1000}, {"station_site_interval_ms", 1000}, {"land_info_interval_ms", 1000}, {"station_site_name_bytes", 64}, {"refit_subtype_page_items", 16}, {"station_waiting_page_items", 64}, {"custom_news_rows", 1024}, {"custom_news_encoded_bytes", 4194304}, {"custom_news_item_encoded_bytes", 65536}, {"custom_news_page_rows", 32}}},
		{"conventions", {{"directions", {"NE", "SE", "SW", "NW"}}, {"entity_handles", "id, generation and opaque engine_instance_id are required; refresh handles after an engine restart; world epoch is required in the bridge envelope"},
			{"direction_coordinates", "Direction values are screen diagonals with map-coordinate offsets: 0 NE=(-1,0), 1 SE=(0,1), 2 SW=(1,0), 3 NW=(0,-1). Road-bit masks are NW=1, SW=2, SE=4, NE=8."},
			{"road_access", "Road depot and bay-stop direction selects the entrance neighbor. Its reciprocal road bit is 2,1,8,4 for directions 0,1,2,3 respectively; matching bits alone do not establish native feasibility or an entire route. For drive_through road stops, directions 0/2 select the X axis (bits10) and 1/3 select the Y axis (bits5). Typed depot/stop construction does not run GUI automatic neighbor-road connection. Compose road.pieces.build or other road commands as needed, with separate native outcomes. Construction success and positive vehicle speed do not establish arrival, loading or delivery."},
			{"tiles", "native tile index or x/y map coordinates"}, {"removal", "terrain.clear demolishes depots, airports, docks, locks, canals and other removable tiles under normal gameplay rules"},
			{"station_creation", "Omitting station_id selects a new station; reusing an active or gray station requires its handle. adjacent=false permits the normal adjacent-station joining search."},
			{"vehicle_parts", "Fleet pages contain at most 256 part records and mark omitted parts with parts_truncated. Use vehicle.details and its consist_version-bound part pagination to inspect longer consists."},
			{"tile_types", {"clear", "railway", "road", "house", "trees", "station", "water", "void", "industry", "tunnel_bridge", "object"}},
			{"track_bits", {"x", "y", "upper", "lower", "left", "right"}}, {"road_bits", {"NW", "SW", "SE", "NE"}},
			{"pagination", "next_after_id is the last examined pool slot; pages may be empty before the end of a sparse pool"},
			{"map_pagination", "map.region uses live row-major pages: repeat the same region with after_offset=next_after_offset until complete=true. Each page has its own page_tick; pages do not form an atomic snapshot."},
			{"bulk_actions", "Larger areas and bulk fleet operations compose individual ordinary actions. Each action has its own outcome; partial completion is possible."},
			{"custom_news", "news.custom is recipient-scoped current-boot custom GS news metadata. Select one news_id with display.text kind news_custom_text; encoded input is retained, visible text uses live native names/language. Cursors bind the company incarnation. References are historical. Retention loss sets gap. No hard per-render work bound."},
			{"question_pagination", "goal.questions returns paired next_after_id and next_after_generation cursors. Omitting after_generation preserves legacy whole-ID skipping. Client-private prompts belong only to the actual native client window."},
			{"observation_intervals", "Text rendering, refit probing, land information, station coverage, linear control-reference selectors and order/page control tokens share one process-global admission interval. Valuation and station waiting have separate process-global intervals. Each admitted call may exceed the cooperative tick slice."},
			{"land_info", "House descriptions, per-tile cargo acceptance and company-specific clearing checks share query-local callback PSA writes. Clear preview retains native QueryCost quota, ownership, cost and town-rating test semantics. Native query scratch and presentation/debug state can change; no funds are checked or reserved."},
			{"station_site", "Selected loaded rail/road specifications and normal extent64 use native QueryCost feasibility and GUI rectangular coverage. Coverage callback PSA writes are query-local. Cargo name previews are plain UTF-8 at most64bytes with name_truncated; full labels use display.text. Admission bounds starts, not callback latency or temporary string expansion. Joined coverage and irregular airport tiles are excluded."},
			{"visual_actions", "Typed actions are shared by text and visual players. Visual game-state perception is genuine PNG capture; structured world queries remain text-only. ui.apply is an optional image-bound native-input path."},
			{"action_estimate", {{"endpoint", "/v1/actions/estimate"}, {"modes", {"text", "visual"}}, {"result_fields", {"success", "cost", "error", "advisory", "test_and_execute_may_differ"}},
				{"semantics", "One current typed action, no request_id or gameplay receipt. Advisory native Shift test only; no execution or funds reservation. NoEst and ui.apply are unavailable. NoTest marks possible test/execution differences. Native auxiliary PSA allocation and monotonic generation effects are allowed; both RNG streams are restored. One estimate start per world per second; no fairness or latency guarantee."}}},
			{"control_reference", {{"parameters", ControlReferenceSchema()}, {"result_fields", {"kind", "reference", "order_version", "page", "page_version", "engine_id"}},
				{"semantics", "Resolve one explicitly selected current target to an actuator reference and opaque mutation tokens only; no attributes or lists. Raw IDs deliberately select the current occupant, including after restart. Never silently refresh a prior action or infer cross-boot identity."}}}}},
		{"human_only", {"client display preferences and GUI configuration"}}};
}

Json FormatCommandResult(Commands command, const CommandCost &cost, const CommandDataBuffer &return_data, const Json &action)
{
	Json result = {{"success", cost.Succeeded()}, {"cost", cost.GetCost().base()}};
	if (cost.Failed()) {
		/* Only normal, human-visible gameplay feedback is projected. Never expose raw buffers. */
		result["error"] = {{"code", "gameplay_rejected"}, {"message", "The game rejected the action"}};
		CommandCost copy = cost;
		if (!copy.GetEncodedMessage().empty()) result["error"]["message"] = copy.GetEncodedMessage().GetDecodedString();
		else if (cost.GetErrorMessage() != INVALID_STRING_ID) result["error"]["message"] = GetString(cost.GetErrorMessage());
		auto &message = result["error"]["message"].get_ref<std::string &>();
		if (message.size() > 1024) {
			size_t end = 1024;
			while (end > 0 && (static_cast<unsigned char>(message[end]) & 0xC0) == 0x80) --end;
			message.resize(end);
		}
		try { (void)Json(message).dump(); }
		catch (const Json::exception &) { message = "The game rejected the action"; }
		return result;
	}
	if (action.is_object() && action.contains("name") && action.contains("parameters") && action.at("parameters").contains("tile")) {
		const std::string name = action.at("name").get<std::string>();
		const TileIndex tile = Parameters(action.at("parameters")).Tile("tile");
		if (name == "road.depot.build" || name == "rail.depot.build" || name == "water.depot.build") {
			const bool expected_depot = (name == "road.depot.build" && command == Commands::BuildRoadDepot && IsRoadDepotTile(tile)) ||
				(name == "rail.depot.build" && command == Commands::BuildRailDepot && IsRailDepotTile(tile)) ||
				(name == "water.depot.build" && IsDepotTile(tile) && !IsHangarTile(tile));
			if (expected_depot && IsTileOwner(tile, _current_company)) {
				if (const Depot *depot = Depot::GetIfValid(GetDepotIndex(tile))) result["depot_id"] = Handle(depot);
				if (name == "road.depot.build") result["direction"] = to_underlying(GetRoadDepotDirection(tile));
				if (name == "rail.depot.build") result["direction"] = to_underlying(GetRailDepotDirection(tile));
			}
		} else if (name == "road.stop.build" || name == "road.waypoint.build" || name == "rail.station.build" || name == "rail.waypoint.build" ||
				name == "water.dock.build" || name == "water.buoy.build" || name == "airport.build") {
			if (IsTileType(tile, TileType::Station)) {
				const BaseStation *station = BaseStation::GetIfValid(GetStationIndex(tile));
				if (station != nullptr && (station->owner == _current_company || (name == "water.buoy.build" && station->owner == OWNER_NONE))) result["station_id"] = Handle(station);
			}
		}
	}
	if (command == Commands::CreateGroup && return_data.size() >= sizeof(GroupID)) {
		EndianBufferReader reader(return_data); GroupID id; reader >> id;
		if (const Group *group = Group::GetIfValid(id)) result["group_id"] = Handle(group);
	}
	if (command == Commands::PlaceSign && return_data.size() >= sizeof(SignID)) {
		EndianBufferReader reader(return_data); SignID id; reader >> id;
		if (const Sign *sign = Sign::GetIfValid(id)) result["sign_id"] = Handle(sign);
	}
	if (command == Commands::FoundTown && return_data.size() >= sizeof(Money) + sizeof(TownID)) {
		EndianBufferReader reader(return_data); Money founding_cost; TownID id; reader >> founding_cost >> id;
		if (const Town *town = Town::GetIfValid(id)) result["town_id"] = Handle(town);
	}
	if (command == Commands::BuildVehicle || command == Commands::CloneVehicle) {
		if (return_data.size() < sizeof(VehicleID)) return result;
		EndianBufferReader reader(return_data);
		VehicleID id;
		reader >> id;
		const Vehicle *vehicle = Vehicle::GetIfValid(id);
		if (vehicle != nullptr) result["vehicle_id"] = Handle(vehicle);
		if (command == Commands::BuildVehicle && return_data.size() >= sizeof(VehicleID) + sizeof(uint) + sizeof(uint16_t) + NUM_CARGO * sizeof(uint)) {
			uint capacity;
			uint16_t mail_capacity;
			CargoArray capacities;
			reader >> capacity >> mail_capacity >> capacities;
			result["capacity"] = capacity;
			result["mail_capacity"] = mail_capacity;
			result["cargo_capacities"] = Json::array();
			for (uint cargo = 0; cargo < NUM_CARGO; ++cargo) if (capacities[static_cast<CargoType>(cargo)] != 0) result["cargo_capacities"].push_back({{"cargo_id", cargo}, {"capacity", capacities[static_cast<CargoType>(cargo)]}});
		}
	}
	if (command == Commands::RefitVehicle && return_data.size() >= sizeof(uint) + sizeof(uint16_t) + NUM_CARGO * sizeof(uint)) {
		EndianBufferReader reader(return_data);
		uint capacity;
		uint16_t mail_capacity;
		CargoArray capacities;
		reader >> capacity >> mail_capacity >> capacities;
		result["capacity"] = capacity;
		result["mail_capacity"] = mail_capacity;
		result["cargo_capacities"] = Json::array();
		for (uint cargo = 0; cargo < NUM_CARGO; ++cargo) if (capacities[static_cast<CargoType>(cargo)] != 0) result["cargo_capacities"].push_back({{"cargo_id", cargo}, {"capacity", capacities[static_cast<CargoType>(cargo)]}});
	}
	return result;
}

bool IsAllowedNativeCommand(Commands command)
{
	return FindNative(command) != nullptr;
}

bool IsNativeCommandForAction(const std::string &action, Commands command)
{
	for (const auto &spec : ACTIONS) {
		if (action != spec.name) continue;
		for (const auto &native : spec.native) if (native.command == command) return true;
		return false;
	}
	return false;
}

std::vector<TileIndex> NativeIntentTiles(Commands command, const CommandDataBuffer &data)
{
	const NativeCommandSpec *native = FindNative(command);
	if (native == nullptr) throw InvalidParameters("Native command is outside the gameplay allowlist");
	auto intent = native->describe(data);
	return {intent.footprint.begin(), intent.footprint.end()};
}

static ActionResult InspectNativeDescription(Commands command, const CommandDataBuffer &data, const NativeDescription &intent, const TargetWatermarks &watermarks, ClientID owner)
{
	try {
		RequireCompany();
		if (Company::Get(_current_company)->GetInstanceGeneration() > watermarks.company) throw InvalidParameters("Company did not exist in the source frame");
		if (auto story = ValidateStoryNativeTargets(command, data, watermarks, owner)) return *story;
		std::vector<uint64_t> inspected((Vehicle::GetPoolSize() + 63) / 64);
		auto first_visit = [&](VehicleID id) {
			if (id.base() >= Vehicle::GetPoolSize()) throw InvalidParameters("Native vehicle index is outside its pool");
			auto &word = inspected[id.base() / 64];
			uint64_t mask = uint64_t{1} << (id.base() % 64);
			bool first = (word & mask) == 0; word |= mask; return first;
		};
		for (auto id : intent.vehicles) {
			const Vehicle *vehicle = Vehicle::GetIfValid(id);
			if (vehicle == nullptr || vehicle->owner != _current_company || !IsCompanyBuildableVehicleType(vehicle)) throw InvalidParameters("Native vehicle target is invalid");
			uint count = 0;
			for (const Vehicle *part : NativeConsistParts(vehicle)) {
				if (++count > NATIVE_MAX_CONSIST_PARTS) throw InvalidParameters("Native consist exceeds the native train-length bound");
				if (first_visit(part->index) && part->GetInstanceGeneration() > watermarks.vehicle) throw InvalidParameters("Vehicle incarnation is newer than the source frame");
			}
			/* An order list can affect shared peers beyond the selected consist. */
			bool order_cohort = command == Commands::InsertOrder || command == Commands::DeleteOrder || command == Commands::SkipToOrder ||
				command == Commands::MoveOrder || command == Commands::CloneOrder || command == Commands::CloneVehicle || command == Commands::ModifyOrder ||
				command == Commands::OrderRefit || command == Commands::ChangeTimetable || command == Commands::SetVehicleOnTime ||
				command == Commands::AutofillTimetable || command == Commands::SetTimetableStart || intent.group_move_shared;
			if (order_cohort) {
				uint shared_count = 0;
				for (const Vehicle *peer : NativeSharedVehicles(vehicle)) {
					if (++shared_count > NATIVE_MAX_SHARED_VEHICLES) throw InvalidParameters("Native shared cohort exceeds the inspection budget");
					if (peer->owner != _current_company || !peer->IsPrimaryVehicle() || peer->GetInstanceGeneration() > watermarks.vehicle) throw InvalidParameters("Shared vehicle incarnation is newer than the source frame or invalid");
					if (intent.group_move_shared) {
						uint part_count = 0;
						for (const Vehicle *part : NativeConsistParts(peer)) {
							if (++part_count > NATIVE_MAX_CONSIST_PARTS) throw InvalidParameters("Native consist exceeds the native train-length bound");
							if (first_visit(part->index) && part->GetInstanceGeneration() > watermarks.vehicle) throw InvalidParameters("Shared consist incarnation is newer than the source frame");
						}
					}
				}
			}
		}
		for (auto id : intent.stations) {
			const BaseStation *station = BaseStation::GetIfValid(id);
			if (station == nullptr || station->GetInstanceGeneration() > watermarks.station) throw InvalidParameters("Station incarnation is newer than the source frame or invalid");
		}
		for (auto id : intent.depots) {
			const Depot *depot = Depot::GetIfValid(id);
			if (depot == nullptr || depot->GetInstanceGeneration() > watermarks.depot) throw InvalidParameters("Depot incarnation is newer than the source frame or invalid");
		}
		for (auto id : intent.companies) {
			const Company *company = Company::GetIfValid(id);
			if (company == nullptr || company->GetInstanceGeneration() > watermarks.company) throw InvalidParameters("Recipient company incarnation is newer than the source frame or invalid");
		}
		std::set<GroupID> groups;
		std::vector<GroupID> pending_groups(intent.groups.begin(), intent.groups.end());
		while (!pending_groups.empty()) {
			auto id = pending_groups.back(); pending_groups.pop_back();
			if (!groups.insert(id).second) continue;
			const Group *group = Group::GetIfValid(id);
			if (group == nullptr || group->owner != _current_company || group->GetInstanceGeneration() > watermarks.group) throw InvalidParameters("Group incarnation is newer than the source frame or invalid");
			/* Native hierarchy effects can reach children not present in arguments. */
			if (intent.descendant_groups) {
				for (auto child : group->children) pending_groups.push_back(child);
			}
		}
		/* These references deliberately do not become descendant roots. */
		for (GroupID id : intent.reference_groups) {
			const Group *group = Group::GetIfValid(id);
			if (group == nullptr || group->owner != _current_company || group->GetInstanceGeneration() > watermarks.group) throw InvalidParameters("Referenced group incarnation is newer than the source frame or invalid");
		}
		if (command == Commands::DeleteGroup) {
			for (const Vehicle *vehicle : Vehicle::Iterate()) {
				if (vehicle->owner != _current_company || !vehicle->IsPrimaryVehicle() || !groups.contains(vehicle->group_id)) continue;
				uint part_count = 0;
				for (const Vehicle *part : NativeConsistParts(vehicle)) {
					if (++part_count > NATIVE_MAX_CONSIST_PARTS) throw InvalidParameters("Native consist exceeds the native train-length bound");
					if (part->GetInstanceGeneration() > watermarks.vehicle) throw InvalidParameters("A grouped vehicle part is newer than the source frame");
				}
			}
		}
		for (auto id : intent.towns) {
			const Town *town = Town::GetIfValid(id);
			if (town == nullptr || town->GetInstanceGeneration() > watermarks.town) throw InvalidParameters("Town incarnation is newer than the source frame or invalid");
		}
		for (auto id : intent.signs) {
			const Sign *sign = Sign::GetIfValid(id);
			if (sign == nullptr || sign->GetInstanceGeneration() > watermarks.sign) throw InvalidParameters("Sign incarnation is newer than the source frame or invalid");
		}
		for (auto id : intent.objects) {
			const Object *object = Object::GetIfValid(id);
			if (object == nullptr || object->GetInstanceGeneration() > watermarks.object) throw InvalidParameters("Object incarnation is newer than the source frame or invalid");
		}
		for (auto id : intent.engines) {
			const Engine *engine = Engine::GetIfValid(id);
			if (engine == nullptr || engine->GetInstanceGeneration() > watermarks.engine) throw InvalidParameters("Engine incarnation is newer than the source frame or invalid");
		}
		return {true, {}, {}};
	} catch (const std::exception &error) {
		return {false, error.what(), "invalid_native_target"};
	}
}

static Json NativeDescriptionState(Commands command, const CommandDataBuffer &data, const NativeDescription &intent, ClientID owner)
{
	Json state = Json::object();
	if (intent.implicit_wagon_move.has_value()) state["implicit_wagon_move"] = *intent.implicit_wagon_move;
	if (auto proof = StoryIntentState(command, data, owner)) state["story_state"] = std::move(*proof);
	if (auto proof = AccessIntentState(command, data)) state["access_state"] = std::move(*proof);
	if (auto proof = OrdersIntentState(command, data)) state["order_state"] = std::move(*proof);
	if (auto proof = ManagementIntentState(command, data)) state["management_state"] = std::move(*proof);
	if (auto proof = ConstructionIntentState(command, data)) state["construction_state"] = std::move(*proof);
	if (intent.industry_platform_dependencies) state["industry_platform_dependencies"] = *intent.industry_platform_dependencies;
	if (auto proof = CompanyIntentState(command, data)) state["company_state"] = std::move(*proof);
	auto hex = [](const CommandDataBuffer &bytes) {
		static constexpr char DIGITS[] = "0123456789abcdef";
		std::string text; text.reserve(bytes.size() * 2);
		for (uint8_t byte : bytes) { text.push_back(DIGITS[byte >> 4]); text.push_back(DIGITS[byte & 15]); }
		return text;
	};
	bool order_state = !state.contains("order_state") && (command == Commands::InsertOrder || command == Commands::DeleteOrder || command == Commands::SkipToOrder || command == Commands::MoveOrder || command == Commands::CloneOrder || command == Commands::CloneVehicle);
	bool consist_state = command == Commands::MoveRailVehicle || command == Commands::RefitVehicle || command == Commands::SellVehicle || command == Commands::CloneVehicle || command == Commands::ReverseTrainDirection || command == Commands::BuildVehicle;
	crypto_blake2b_ctx consist_hash;
	crypto_blake2b_init(&consist_hash, 32);
	auto append_consist = [&](const auto &value) {
		auto bytes = EndianBufferWriter<CommandDataBuffer>::FromValue(value);
		crypto_blake2b_update(&consist_hash, bytes.data(), bytes.size());
	};
	append_consist(std::string("OpenTTD integration consist v2"));
	uint32_t consist_roots = 0, consist_parts = 0;
	std::set<VehicleID> seen;
	for (auto id : intent.vehicles) {
		if (!seen.insert(id).second) continue;
		const Vehicle *vehicle = Vehicle::GetIfValid(id);
		if (vehicle == nullptr) throw InvalidParameters("Native vehicle target is invalid");
		if (order_state) {
			Json orders = Json::array();
			uint position = 0;
			for (const Order &order : vehicle->Orders()) {
				if (!order.IsType(OT_IMPLICIT)) orders.push_back({{"position", position}, {"bytes", hex(EndianBufferWriter<CommandDataBuffer>::FromValue(order))}});
				++position;
			}
			state["orders"].push_back({{"vehicle_id", id.base()}, {"count", orders.size()}, {"manual", std::move(orders)}});
		}
		if (command == Commands::CloneVehicle) state["clone_membership"].push_back({{"vehicle_id", id.base()}, {"group", vehicle->group_id.base()}});
		if (command == Commands::StartStopVehicle) state["toggle"].push_back({{"vehicle_id", id.base()}, {"stopped", vehicle->vehstatus.Test(VehState::Stopped)}, {"crashed", vehicle->vehstatus.Test(VehState::Crashed)}});
		if (consist_state) {
			append_consist(uint8_t{1}); append_consist(id.base());
			++consist_roots;
			uint32_t count = 0;
			for (const Vehicle *part : NativeConsistParts(vehicle)) {
				if (++count > NATIVE_MAX_CONSIST_PARTS) throw InvalidParameters("Native consist exceeds the native train-length bound");
				append_consist(uint8_t{1}); // exact part-record boundary
				append_consist(part->index.base()); append_consist(part->engine_type.base());
				append_consist(to_underlying(part->cargo_type)); append_consist(part->cargo_cap);
				append_consist(part->type == VehicleType::Train);
				if (part->type == VehicleType::Train) {
					const auto &flags = Train::From(part)->flags;
					append_consist(flags.Test(VehicleRailFlag::Reversed));
					append_consist(flags.Test(VehicleRailFlag::Flipped));
					append_consist(flags.Test(VehicleRailFlag::Reversing));
				}
			}
			append_consist(uint8_t{0}); append_consist(count);
			consist_parts += count;
		}
	}
	if (consist_state) {
		append_consist(uint8_t{0}); append_consist(consist_roots); append_consist(consist_parts);
		std::array<uint8_t, 32> digest;
		crypto_blake2b_final(&consist_hash, digest.data());
		state["consists"] = {{"root_count", consist_roots}, {"part_count", consist_parts}, {"digest", FormatArrayAsHex(digest)}};
	}
	if (command == Commands::OpenCloseAirport) {
		for (auto id : intent.stations) {
			const Station *station = Station::GetIfValid(id);
			if (station == nullptr || !station->facilities.Test(StationFacility::Airport)) throw InvalidParameters("Native airport target is invalid");
			state["airport_toggle"].push_back({{"station_id", id.base()}, {"closed", station->airport.blocks.Test(AirportBlock::AirportClosed)}});
		}
	}
	if (!intent.reuse_candidates.empty()) {
		crypto_blake2b_ctx hash;
		crypto_blake2b_init(&hash, 32);
		auto append = [&](const auto &value) {
			auto bytes = EndianBufferWriter<CommandDataBuffer>::FromValue(value);
			crypto_blake2b_update(&hash, bytes.data(), bytes.size());
		};
		append(std::string("OpenTTD integration deleted station candidates v2"));
		append(static_cast<uint32_t>(intent.reuse_candidates.size()));
		for (auto id : intent.reuse_candidates) {
			/* Pin position as well as identity: position changes the closest match. */
			append(id.base()); append(BaseStation::Get(id)->xy.base());
		}
		std::array<uint8_t, 32> digest;
		crypto_blake2b_final(&hash, digest.data());
		state["deleted_sign_reuse"] = {{"count", intent.reuse_candidates.size()}, {"digest", FormatArrayAsHex(digest)}};
	}
	if (state.dump().size() > NATIVE_MAX_OPERATION_STATE_BYTES) throw InvalidParameters("Native operation state exceeds the 16 KiB budget");
	return state;
}


ActionResult InspectNativeTargets(Commands command, const CommandDataBuffer &data, const TargetWatermarks &watermarks, ClientID owner)
{
	try {
		const auto *native = FindNative(command);
		if (native == nullptr) throw InvalidParameters("Native command is outside the gameplay allowlist");
		auto intent = native->describe(data);
		return InspectNativeDescription(command, data, intent, watermarks, owner);
	} catch (const std::exception &error) { return {false, error.what(), "invalid_native_target"}; }
}

Json NativeIntentState(Commands command, const CommandDataBuffer &data, ClientID owner)
{
	const auto *native = FindNative(command);
	if (native == nullptr) throw InvalidParameters("Native command is outside the gameplay allowlist");
	auto intent = native->describe(data);
	return NativeDescriptionState(command, data, intent, owner);
}

NativeIntentProof CaptureNativeIntentProof(Commands command, const CommandDataBuffer &data, ClientID owner)
{
	const auto *native = FindNative(command);
	if (native == nullptr) throw InvalidParameters("Native command is outside the gameplay allowlist");
	auto intent = native->describe(data);
	return {NativeSpatialProof(command, data, intent.footprint), NativeDescriptionState(command, data, intent, owner)};
}

ActionResult VerifyNativeIntentProof(Commands command, const CommandDataBuffer &data, const TargetWatermarks &watermarks, const Json &tile_proof, const Json &state, ClientID owner)
{
	try {
		if (!tile_proof.is_object() || tile_proof.size() != 2 || !tile_proof.contains("count") || !tile_proof.contains("digest") ||
			!tile_proof.at("count").is_number_unsigned() || tile_proof.at("count").get<uint64_t>() > Map::Size() ||
			!tile_proof.at("digest").is_string()) throw InvalidParameters("Invalid native spatial proof");
		const auto &digest = tile_proof.at("digest").get_ref<const std::string &>();
		if (digest.size() != 64 || !std::ranges::all_of(digest, [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); })) throw InvalidParameters("Invalid native spatial digest");
		if (!state.is_object() || state.dump().size() > NATIVE_MAX_OPERATION_STATE_BYTES) throw InvalidParameters("Invalid native operation proof");
		const auto *native = FindNative(command);
		if (native == nullptr) throw InvalidParameters("Native command is outside the gameplay allowlist");
		auto intent = native->describe(data);
		auto targets = InspectNativeDescription(command, data, intent, watermarks, owner);
		if (!targets.accepted) return targets;
		if (command == Commands::GoalQuestionAnswer) {
			if (intent.footprint.size() != 0 || NativeSpatialProof(command, data, intent.footprint) != tile_proof ||
					state.size() != 1 || !state.contains("story_state")) throw InvalidParameters("Invalid native question presentation proof");
			/* The actual clicked popup is witnessed by the authenticated renderer.
			 * Its mutable z-order cannot be reconstructed from a company inbox. */
			return ValidateStoryQuestionWitness(data, state.at("story_state"), owner);
		}
		if (NativeSpatialProof(command, data, intent.footprint) != tile_proof || NativeDescriptionState(command, data, intent, owner) != state) throw InvalidParameters("Native intent changed after its source frame");
		return {true, {}, {}};
	} catch (const std::exception &error) { return {false, error.what(), "invalid_native_target"}; }
}

} // namespace Integration
