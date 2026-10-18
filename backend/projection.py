"""Positive DTOs for public text results and chat."""

import re
import unicodedata

from .catalog import query_spec
from .errors import ServiceError
from .json_tools import dumps

_HANDLE = {"id": int, "generation": int, "engine_instance_id": str}
_NULLABLE_HANDLE = {"anyOf": [_HANDLE, type(None)]}
_NULLABLE_INT = (int, type(None))
_ERROR = {"code": str, "message": str, "game_message": str, "string_id": int}
_NULLABLE_REPORT_ERROR = {"anyOf": [{"code": str, "message": str}, type(None)]}
_TILE = {"tile": int, "x": int, "y": int}
_NULLABLE_TILE = {"anyOf": [_TILE, type(None)]}
_CAPACITY = {"cargo_id": int, "capacity": int}
_OUTCOME = {
    "success": bool, "cost": int, "error": _ERROR, "vehicle_id": _HANDLE,
    "group_id": _HANDLE, "sign_id": _HANDLE, "town_id": _HANDLE,
    "capacity": int, "mail_capacity": int, "cargo_capacities": [_CAPACITY],
    "station_id": _HANDLE, "depot_id": _HANDLE,
    "order_version": str, "source_order_version": str, "page_version": str,
}
_VISUAL_OUTCOME = {key: shape for key, shape in _OUTCOME.items()
                   if key not in {"capacity", "mail_capacity", "cargo_capacities"}}
_CONTROL_REFERENCE_IDS = {
    "vehicle": 0xFEFFF, "station": 63999, "waypoint": 63999, "depot": 63999,
    "group": 63999, "town": 63999, "sign": 63999, "company": 14,
    "story_page": 63999, "story_element": 63999, "question": 65535,
}
_ORDER_VERSION = re.compile(r"ov1:[0-9A-Fa-f]{64}\Z")
_PAGE_VERSION = re.compile(r"spv1:[0-9A-Fa-f]{64}\Z")
_PERSON = {"identity_id": str, "company_id": (int, str), "kind": str, "display_name": str}
_CHAT = {
    "id": str, "message_id": str, "thread_id": str, "scope": str,
    "sender": _PERSON, "recipients": [_PERSON], "body": str, "timestamp": str,
    "tick": int, "tick_authoritative": bool,
}
_SIGNAL = {"track": int, "signal_type": int, "semaphore": bool}
_MAP_TILE = {
    **_TILE, "type": int, "height": int, "slope": int, "owner": (int, type(None)),
    "station_id": _HANDLE, "station_type": int, "hangar": bool, "industry_id": int,
    "water_depot": bool, "lock": bool, "canal": bool, "depot_axis": str,
    "road_type": int, "tram_type": int, "depot": bool, "road_bits": int,
    "tram_bits": int, "disallowed_road_directions": int, "depot_direction": int,
    "rail_type": int, "track_bits": int, "has_signals": bool,
    "present_signals": int, "signal_states": int, "signals": [_SIGNAL],
    "transport": str, "tunnel": bool, "other_end": _TILE,
    "direction": int, "station_track": int, "crossing_track": int,
    "lock_direction": int, "dock_direction": int,
}
_VEHICLE_PART = {"vehicle_id": _HANDLE, "engine_id": int, "cargo_id": int, "capacity": int, "load": int}
_VEHICLE = {
    "vehicle_id": _HANDLE, "vehicle_type": str, "engine_id": int, "location": _TILE,
    "speed": int, "maximum_speed": int, "stopped": bool, "crashed": bool,
    "profit_this_year": int, "profit_last_year": int, "running_cost": int,
    "order_count": int, "current_order": int, "parts": [_VEHICLE_PART], "parts_truncated": bool,
    "group_id": _NULLABLE_HANDLE, "service_interval": int,
    "service_custom": bool, "service_percent": bool, "force_proceed": int,
}
_STATION_CARGO = {"cargo_id": int, "waiting": int, "has_rating": bool, "rating": int}
_STATION = {
    "station_id": _HANDLE, "name": str, "location": _TILE,
    "rail": bool, "bus": bool, "truck": bool, "dock": bool, "airport": bool,
    "cargo": [_STATION_CARGO],
    "airport_details": {"airport_type": int, "layout": int, "hangars": [_TILE]},
}
_GROUP = {
    "group_id": _HANDLE, "parent_group_id": _NULLABLE_HANDLE,
    "name": str, "vehicle_type": str,
    "replace_protection": bool, "wagon_removal": bool,
    "vehicle_count": int, "profit_last_year": int,
    "livery": {"primary": int, "secondary": int,
               "primary_override": bool, "secondary_override": bool},
}
_AUTOREPLACE_RULE = {"old_engine_id": int, "new_engine_id": int, "when_old": bool}
_ORDER = {
    "position": int, "type": int, "implicit": bool,
    "destination_id": int, "load": int, "unload": int,
    "non_stop": int, "refit_cargo_id": int,
    "kind": str, "routing": str, "load_policy": str,
    "unload_policy": str, "stop_location": str, "nearest_depot": bool,
    "depot_action": str, "destination": _TILE,
    "condition": {"variable": str, "comparator": str,
                  "value": int, "target_position": int},
    "refit": {"mode": str, "cargo_id": int},
    "wait_ticks": int, "travel_ticks": int,
    "wait_timetabled": bool, "travel_timetabled": bool,
    "speed_limit": _NULLABLE_INT,
    "destination_entity": {"kind": str, "handle": _HANDLE},
}
_ENGINE = {
    "engine_id": int, "name": str, "vehicle_type": str, "price": int,
    "running_cost": int, "maximum_speed": int, "default_cargo_id": int,
    "capacities": [_CAPACITY], "refittable_cargo_ids": [int],
    "road_type": int, "rail_types": [int], "wagon": bool,
    "capacity": int, "mail_capacity": int, "aircraft_subtype": int, "range": int,
    "hidden_for_company": bool,
}
_AIRPORT = {
    "airport_type": int, "name": str, "width": int, "height": int,
    "layout_count": int, "coverage_radius": int, "noise": int,
}
_CARGO = {"cargo_id": int, "name": str, "freight": bool}
_RAILTYPE = {"rail_type": int, "name": str}
_ROADTYPE = {"road_type": int, "name": str, "tram": bool}
_TOWN = {"town_id": int, "town": _HANDLE, "name": str, "location": _TILE, "population": int}
_INDUSTRY = {
    "industry_id": int, "industry": _HANDLE, "name": str,
    "name_truncated": bool,
    "location": _TILE, "width": int, "height": int,
    "accepted_cargo_ids": [int],
    "produced_cargo": [{"cargo_id": int, "waiting": int, "production_rate": int}],
}
_QUERY_BASE = {"tick": int, "company_id": int}
_INDUSTRY_DETAILS = {
    **_QUERY_BASE, "industry_id": _HANDLE, "name": str,
    "name_truncated": bool,
    "industry_type": int, "location": _TILE, "width": int, "height": int,
    "nearest_town": _NULLABLE_HANDLE, "announced_closure": bool,
    "period_unit": str, "accepted_cargo_ids": [int],
    "produced_cargo": [{"cargo_id": int, "waiting": int,
                        "production_rate": int}],
    "accepted": [{"slot": int, "cargo_id": int,
                  "input_waiting": _NULLABLE_INT,
                  "input_waiting_visibility": str,
                  "input_waiting_unavailable": bool,
                  "delivered_last_period": _NULLABLE_INT,
                  "waiting_average_last_period": _NULLABLE_INT}],
    "produced": [{"slot": int, "cargo_id": int,
                  "produced_last_period": _NULLABLE_INT,
                  "transported_last_period": _NULLABLE_INT,
                  "transported_percent_last_period": _NULLABLE_INT}],
    "omitted_presentation": {"cargo_suffix_callback": bool,
                             "window_more_text_callback": bool,
                             "custom_text": bool},
}
_INDUSTRY_ACCEPTED_POINT = {"periods_ago": int, "delivered": _NULLABLE_INT,
                            "waiting_average": _NULLABLE_INT}
_INDUSTRY_PRODUCED_POINT = {"periods_ago": int, "produced": _NULLABLE_INT,
                            "transported": _NULLABLE_INT}
_INDUSTRY_MONTHLY_STATS = {
    **_QUERY_BASE, "industry_id": _HANDLE, "cargo_id": int,
    "direction": str, "period_unit": str, "points": [_INDUSTRY_ACCEPTED_POINT],
}
_DISPLAY_TEXT_TARGET_KINDS = frozenset({
    "town_text", "subsidy_cargo_name", "subsidy_source_name",
    "subsidy_destination_name", "league_table_title", "league_table_header",
    "league_table_footer", "league_entry_text", "league_entry_score",
    "goal_text", "goal_progress", "industry_extra_text",
    "industry_name", "industry_custom_text",
})
_DISPLAY_TEXT_REFERENCE_FIELDS = frozenset({
    "target", "vehicle_id", "cargo_id", "subtype", "direction", "slot",
})
_DISPLAY_TEXT = {
    **_QUERY_BASE, "kind": str, "target": _HANDLE,
    "vehicle_id": _HANDLE, "cargo_id": int, "subtype": int,
    "direction": str, "slot": int,
    "text": str, "content_version": str, "total_bytes": int,
    "next_after_byte": _NULLABLE_INT,
}
_WORLD_DATE = {"date_day": int, "year": int, "month": int, "day": int, "day_fraction": int}
_WORLD = {
    **_QUERY_BASE,
    "map": {"width": int, "height": int, "climate": str},
    "calendar": _WORLD_DATE, "economy": _WORLD_DATE,
    "timekeeping": {"mode": str, "minutes_per_calendar_year": int,
                    "ticks_per_day": int, "financial_period_unit": str},
    "rules": {
        "station_spread": int, "modified_catchment": bool,
        "serve_neutral_industries": bool, "distant_join_stations": bool,
        "station_noise_level": bool, "infrastructure_maintenance": bool,
        "local_authority_distance": int, "max_train_length": int,
        "vehicle_limits": {"train": int, "road": int, "aircraft": int, "ship": int},
        "cargo_distribution": {"passengers": str, "mail": str,
                               "armoured": str, "default": str},
    },
}
_LAND_OWNER = {"slot": int, "kind": str, "company_id": _NULLABLE_INT,
               "town_id": _NULLABLE_HANDLE, "label": str}
_LAND_INFO = {
    **_QUERY_BASE, "tile": _TILE, "tile_type": int, "height": int,
    "slope": int, "description": str, "owners": [_LAND_OWNER],
    "local_authority": _NULLABLE_HANDLE,
    "build_date": {"anyOf": [{"year": int, "month": int, "day": int}, type(None)]},
    "house": {"anyOf": [{"house_type": int, "name": str}, type(None)]},
    "object": {"anyOf": [{"object_type": int, "name": str}, type(None)]},
    "station_spec": {"anyOf": [{"class_id": int, "spec_index": int, "name": str}, type(None)]},
    "accepted_cargo": [{"cargo_id": int, "acceptance_eighths": int,
                        "always_accepted": bool}],
    "clear_preview": {"status": str, "cost": _NULLABLE_INT,
                      "error": _NULLABLE_REPORT_ERROR, "advisory_tick": int},
}
_STATION_SITE = {
    **_QUERY_BASE, "build_action": str,
    "selection": {"kind": str, "tile": _TILE, "spec_class": _NULLABLE_INT,
                  "spec_index": _NULLABLE_INT, "airport_type": _NULLABLE_INT,
                  "layout": _NULLABLE_INT, "rotation": (str, type(None))},
    "footprint": {"x": int, "y": int, "width": int, "height": int,
                  "tile_count": int},
    "catchment": {"radius": int, "coverage_model": str,
                  "joined_coverage_excluded": bool,
                  "irregular_airport_tiles_modeled": bool},
    "accepted_cargo": [{"cargo_id": int, "name": str, "name_truncated": bool,
                        "acceptance_eighths": int, "preview_threshold_met": bool,
                        "facility_eligible": bool, "always_accepted": bool}],
    "supplied_cargo": [{"cargo_id": int, "name": str, "name_truncated": bool,
                        "source_count": int,
                        "preview_threshold_met": bool}],
    "authority": {"town_id": _NULLABLE_HANDLE, "station_allowed": bool,
                  "airport_noise": {"anyOf": [
                      {"enabled": bool, "current": int, "increment": int, "limit": int},
                      type(None)]}},
    "feasibility": {"status": str, "cost": _NULLABLE_INT,
                    "error": _NULLABLE_REPORT_ERROR, "advisory_tick": int,
                    "cash_checked": bool, "reserved": bool,
                    "build_result_guaranteed": bool},
}
_TOWN_AUTHORITY = {
    **_QUERY_BASE, "town_id": _HANDLE, "name": str, "location": _TILE,
    "population": int,
    "ratings": [{"company_id": int, "category": str, "exclusive": bool}],
    "viewer_rating": {"present": bool, "category": (str, type(None))},
    "exclusivity": {"anyOf": [
        {"company_id": int, "remaining_periods": int}, type(None)]},
    "viewer_unwanted_periods": int, "viewer_has_statue": bool,
    "airport_noise": {"anyOf": [
        {"enabled": bool, "current": int, "limit": int}, type(None)]},
    "actions": [{"name": str, "enabled_by_rules": bool,
                 "available_now": bool, "advisory_cost": int}],
}
_TOWN_DETAIL = {
    **_QUERY_BASE, "town_id": _HANDLE, "name": str, "name_truncated": bool,
    "location": _TILE, "city": bool, "population": int, "house_count": int,
    "text": str, "text_truncated": bool,
    "supplied_cargo": [{"cargo_id": int, "name": str, "name_truncated": bool,
                        "produced_last_period": int, "transported_last_period": int}],
    "growth_requirements": [{"cargo_id": int, "name": str,
                             "name_truncated": bool, "kind": str, "status": str,
                             "delivered_last_period": _NULLABLE_INT,
                             "required_amount": _NULLABLE_INT}],
    "growth": {"growing": bool, "interval_days": _NULLABLE_INT,
               "funded": (bool, type(None))},
    "airport_noise": {"anyOf": [{"current": int, "limit": int}, type(None)]},
}
_SUBSIDY_ENDPOINT = {"kind": str, "entity": _HANDLE, "name": str,
                     "name_truncated": bool, "location": _TILE}
_SUBSIDY = {
    "subsidy_id": _HANDLE, "status": str, "cargo_id": int,
    "cargo_name": str, "cargo_name_truncated": bool,
    "source": _SUBSIDY_ENDPOINT, "destination": _SUBSIDY_ENDPOINT,
    "awarded_company": _NULLABLE_HANDLE, "remaining_periods": int,
    "period_unit": str, "expires_economy_date_day": _NULLABLE_INT,
}
_LEAGUE_PERFORMANCE_ROW = {"rank": int, "company": _HANDLE, "name": str,
                           "name_truncated": bool, "title": str,
                           "title_truncated": bool}
_LEAGUE_TABLE = {"table_id": _HANDLE, "title": str,
                 "title_truncated": bool}
_LINK_TARGET = {"anyOf": [{"kind": str, "tile": {"anyOf": [_TILE, type(None)]},
                           "entity": _NULLABLE_HANDLE}, type(None)]}
_LEAGUE_ENTRY = {"entry_id": _HANDLE, "company": _NULLABLE_HANDLE,
                 "text": str, "text_truncated": bool,
                 "score": str, "score_truncated": bool, "link": _LINK_TARGET}
_GOAL = {"goal_id": _HANDLE, "text": str, "text_truncated": bool,
         "progress": str, "progress_truncated": bool,
         "completed": (bool, type(None)), "destination": _LINK_TARGET}
_STATION_DETAILS_CARGO = {
    "cargo_id": int, "accepts": bool, "has_rating": bool,
    "rating_percent": _NULLABLE_INT,
    "waiting": {"available": int, "reserved": int, "total": int},
    "monthly_supply": _NULLABLE_INT,
}
_STATION_DETAILS = {
    **_QUERY_BASE, "station": _HANDLE, "name": str,
    "owner": _NULLABLE_HANDLE, "sign_tile": _NULLABLE_INT,
    "town": _NULLABLE_HANDLE, "facilities": [str],
    "spread": {"anyOf": [{"x": int, "y": int,
                           "width": int, "height": int}, type(None)]},
    "catchment_radius": int,
    "airport": {"anyOf": [{"type_id": int, "layout": int,
                            "rotation": str, "closed": bool,
                            "hangars": int}, type(None)]},
    "cargo": [_STATION_DETAILS_CARGO],
}
_STATION_FLOW_ROW = {
    "source_station_id": int, "source": _NULLABLE_HANDLE,
    "via_station_id": _NULLABLE_INT, "via": _NULLABLE_HANDLE,
    "amount": int, "restricted": bool, "local_consumption": bool,
}
_STATION_WAITING_KEY = {"first_station_id": int,
                        "next_hop_id": _NULLABLE_INT,
                        "packet_units": int}
_STATION_WAITING_ROW = {
    **_STATION_WAITING_KEY, "first_station": _NULLABLE_HANDLE,
    "source_visible_in_gui": bool, "next_hop": _NULLABLE_HANDLE,
    "packet_count": int, "units": int,
}
_STATION_WAITING = {
    **_QUERY_BASE, "station": _HANDLE, "cargo_id": int,
    "distribution_mode": str, "route_details_suppressed": bool,
    "destination_semantics": str, "available_units": int,
    "native_available_cached": int, "native_reserved_cached": int,
    "native_total_cached": int, "available_matches_native_cache": bool,
    "native_total_addition_fits_u32": bool, "aggregate_version": str,
    "rows": [_STATION_WAITING_ROW],
    "next_after_group": {"anyOf": [_STATION_WAITING_KEY, type(None)]},
    "complete": bool, "live": bool,
}
_STATION_LINK_ROW = {
    "to": _HANDLE, "monthly_capacity": int, "monthly_usage": int,
    "travel_days": int, "shared_with_neutral": bool,
}
_FACE_COMPONENT = {"component_id": int, "name": str, "type": str,
                   "value_count": int, "active": bool, "value": int}
_COMPANY_APPEARANCE = {
    **_QUERY_BASE, "president_name": str,
    "manager_face": {"style_id": int, "style_label": str,
                     "components": [_FACE_COMPONENT]},
    "liveries": [{"scheme": str, "primary": str, "secondary": str,
                 "primary_inherited": bool, "secondary_inherited": bool}],
}
_MANAGER_FACE_CATALOG = {
    **_QUERY_BASE, "items": [{"style_id": int, "label": str}],
    "next_after_id": _NULLABLE_INT,
    "selected_style": {"style_id": int, "label": str,
                       "components": [_FACE_COMPONENT]},
}
_REPORT_DATE = {"date": int, "year": int, "month": int, "day": int}
_ECONOMY_QUARTER = {"period": int, "year": int, "quarter": int,
                    "income": int, "expenses": int, "cargo_delivered": int}
_EXPENSE_CATEGORIES = {
    "construction": int, "new_vehicles": int, "train_running": int,
    "road_running": int, "aircraft_running": int, "ship_running": int,
    "property": int, "train_revenue": int, "road_revenue": int,
    "aircraft_revenue": int, "ship_revenue": int, "loan_interest": int,
    "other": int,
}
_COMPANY_REPORT = {
    **_QUERY_BASE, "company": _HANDLE, "name": str, "president_name": str,
    "balance": int, "loan": int, "maximum_loan": int,
    "inaugurated_year": int, "inaugurated_calendar_year": int,
    "vehicle_counts": {"rail": int, "road": int, "ship": int, "aircraft": int},
    "infrastructure": {"rail": int, "road": int, "tram": int,
                       "signals": int, "water": int, "station_tiles": int,
                       "airports": int},
    "calendar_date": _REPORT_DATE,
    "economy_date": {**_REPORT_DATE, "quarter": int, "wallclock": bool},
    "current_quarter": _ECONOMY_QUARTER,
    "quarter_history": [_ECONOMY_QUARTER],
    "yearly_expenses": [{"year": int, "categories": _EXPENSE_CATEGORIES}],
    "acquisition_offer": {"price": int},
}
_COMPANY_VALUATION = {**_QUERY_BASE, "company": _HANDLE,
                      "company_value": int}
_VEHICLE_DETAILS_PART = {
    "position": int, "vehicle_id": _HANDLE, "engine_id": int,
    "cargo_id": _NULLABLE_INT, "cargo_subtype": _NULLABLE_INT,
    "capacity": int, "load": int, "can_carry_cargo": bool,
    "refittable_cargo_ids": [int],
}
_VEHICLE_DETAILS = {
    **_QUERY_BASE, "vehicle_id": _HANDLE, "owner_company_id": int,
    "owned_by_actor": bool, "name": str, "vehicle_type": str,
    "engine_id": int, "location": _NULLABLE_TILE,
    "stopped": bool, "crashed": bool, "in_depot": bool,
    "status": {"kind": str, "text": str, "text_truncated": bool},
    "can_refit_now": bool, "age_calendar_days": int,
    "maximum_age_calendar_days": int, "build_year": int,
    "reliability_percent": int, "engine_reliability_percent": int,
    "breakdowns_since_last_service": int, "running_cost": int,
    "running_cost_unit": str, "profit_this_period": int,
    "profit_last_period": int, "service_interval": int,
    "service_custom": bool, "service_percent": bool,
    "last_service_economy_date": int, "order_count": int,
    "manual_order_count": int, "current_order_position": _NULLABLE_INT,
    "part_count": int, "consist_version": str,
    "parts": [_VEHICLE_DETAILS_PART],
    "next_after_part_position": _NULLABLE_INT,
}
_CARGO_PAYMENT_RATES = {
    **_QUERY_BASE, "cargo_id": int, "amount": int, "distance_tiles": int,
    "money_unit": str, "time_unit": str, "income_scope": str,
    "samples": [{"transit_periods": int, "display_time": int, "income": int}],
}
_CATALOG_BRIDGES = {
    **_QUERY_BASE, "transports": [str], "span_unit": str, "speed_unit": str,
    "items": [{"bridge_type": int, "material": str, "material_truncated": bool,
               "minimum_span_tiles": int, "maximum_span_tiles": int,
               "maximum_speed": _NULLABLE_INT}],
}
_ENGINE_PURCHASE_INFO = {
    **_QUERY_BASE, "engine_id": int, "vehicle_type": str, "speed_unit": str,
    "performance": {
        "designed_year": int, "life_calendar_days": int,
        "reliability_percent": int, "empty_weight_tonnes": int,
        "power_hp": int, "maximum_tractive_effort_newtons": int,
        "powered_wagon_power_hp": int, "powered_wagon_extra_weight_tonnes": int,
        "ocean_speed": int, "canal_speed": int,
    },
}
_VEHICLE_REFIT_SUBTYPES = {
    **_QUERY_BASE, "vehicle_id": _HANDLE, "part_id": _HANDLE,
    "cargo_id": int, "refittable": bool, "current_cargo": bool,
    "keep_option": bool,
    "subtypes": [{"value": int, "label": str,
                  "label_truncated": bool}],
    "next_after_subtype": _NULLABLE_INT, "complete": bool,
    "probe_cap_reached": bool,
}
_STORY_TEXT_CHUNK = {"text": str, "text_offset": int,
                     "text_bytes": int, "next_text_offset": _NULLABLE_INT}
_GOAL_QUESTION = {"question_id": _HANDLE, "type": str,
                  "buttons": [str], "text": str,
                  "text_truncated": bool}
_STORY_PAGE = {"page_id": _HANDLE, "book_company_id": _NULLABLE_INT,
               "sort_order": int, "date": int, "title": str,
               "title_truncated": bool, "interactive": bool}
_STORY_ELEMENT = {"element_id": _HANDLE, "sort_order": int,
                  "type": str, "text": str, "text_truncated": bool,
                  "location": _TILE, "goal_id": _HANDLE,
                  "completed": bool, "vehicle_type": (str, type(None))}
_STORY_VEHICLE_CANDIDATE = {"vehicle_id": _HANDLE,
                            "owner_company_id": int, "vehicle_type": str,
                            "name": str, "location": _TILE}
_ENGINE_PREVIEW_OFFER = {
    "engine_id": int, "name": str, "vehicle_type": str,
    "price": int, "running_cost": int, "maximum_speed": int,
    "default_cargo_id": int, "capacity": int, "mail_capacity": int,
}
_ORDERS_RESULT = {
    **_QUERY_BASE, "vehicle_id": _HANDLE, "order_version": str,
    "order_count": int, "manual_order_count": int, "current_order": int,
    "orders": [_ORDER], "next_after_position": _NULLABLE_INT,
    "shared_count": int, "shared_vehicles": [_HANDLE],
    "next_shared_after_id": _NULLABLE_INT,
    "current_execution": {"type": int, "kind": str,
                          "real_position": _NULLABLE_INT,
                          "destination_entity": {"kind": str,
                                                 "handle": _HANDLE}},
}
_TIMETABLE_RESULT = {
    **_ORDERS_RESULT, "started": bool, "start_tick": int,
    "lateness_ticks": int, "autofill": bool,
    "preserve_wait_time": bool, "complete": bool,
    "duration_ticks": int, "speed_unit": str,
}
_CATALOG_OBJECT = {"object_type": int, "name": str, "width": int,
                   "height": int, "views": int, "base_build_cost": int,
                   "site_costs_extra": bool, "removable": bool,
                   "built_on_water": bool, "water_only": bool}
_CATALOG_HOUSE = {"house_type": int, "name": str, "width": int,
                  "height": int, "population": int,
                  "introduction_year": int, "last_year": int,
                  "placement_enabled": bool, "protected_option": bool}
_CATALOG_INDUSTRY = {"industry_type": int, "name": str, "cost": int,
                     "mode": str, "layout_count": int,
                     "accepts_cargo_ids": [int], "produces_cargo_ids": [int],
                     "available": bool}
_CATALOG_TREE = {"tree_type": int, "name": str, "variant": int,
                 "required_tropic_zone": (str, type(None))}
_CATALOG_STATION_SPEC = {"catalog_id": int, "spec_class": int,
                         "spec_index": int, "class_name": str, "name": str,
                         "available": bool, "disallowed_platforms": int,
                         "disallowed_lengths": int}
_CATALOG_ROADSTOP_SPEC = {"catalog_id": int, "spec_class": int,
                          "spec_index": int, "class_name": str, "name": str,
                          "available": bool, "drive_through_only": bool,
                          "road_only": bool, "tram_only": bool}
_DEPOT = {"depot_id": _HANDLE, "name": str, "location": _TILE,
          "vehicle_type": str}
_WAYPOINT = {"station_id": _HANDLE, "name": str, "location": _TILE,
             "kind": str, "owner_company_id": _NULLABLE_INT}
_SIGN = {"sign_id": _HANDLE, "name": str, "location": _TILE,
         "owner_company_id": _NULLABLE_INT, "editable": bool}


def _page(items):
    return {**_QUERY_BASE, "items": [items], "next_after_id": _NULLABLE_INT}


_QUERY_FIELDS = {
    "goal.questions": {**_QUERY_BASE, "items": [_GOAL_QUESTION],
                       "next_after_id": _NULLABLE_INT,
                       "next_after_generation": _NULLABLE_INT},
    "goal.question": {**_QUERY_BASE, **_GOAL_QUESTION,
                      "text_offset": int, "text_bytes": int,
                      "next_text_offset": _NULLABLE_INT},
    "story.pages": {**_QUERY_BASE, "items": [_STORY_PAGE],
                    "next_after_id": _NULLABLE_INT},
    "story.elements": {**_QUERY_BASE, **_STORY_PAGE,
                       "page_version": str, "title_text": _STORY_TEXT_CHUNK,
                       "items": [_STORY_ELEMENT],
                       "next_after_id": _NULLABLE_INT},
    "story.element": {**_QUERY_BASE, **_STORY_ELEMENT,
                      "page_id": _HANDLE, "page_version": str,
                      "text_offset": int, "text_bytes": int,
                      "next_text_offset": _NULLABLE_INT},
    "story.vehicle_candidates": {**_QUERY_BASE, "page_version": str,
                                 "items": [_STORY_VEHICLE_CANDIDATE],
                                 "next_after_id": _NULLABLE_INT},
    "vehicle.details": _VEHICLE_DETAILS,
    "cargo.payment_rates": _CARGO_PAYMENT_RATES,
    "catalog.bridges": _CATALOG_BRIDGES,
    "engine.purchase_info": _ENGINE_PURCHASE_INFO,
    "vehicle.refit_subtypes": _VEHICLE_REFIT_SUBTYPES,
    "company.valuation": _COMPANY_VALUATION,
    "company.access": {**_QUERY_BASE, "join_policy": str, "participant": _HANDLE,
                       "listed": bool, "permitted": bool},
    "catalog.objects": _page(_CATALOG_OBJECT),
    "catalog.houses": _page(_CATALOG_HOUSE),
    "catalog.industries": _page(_CATALOG_INDUSTRY),
    "catalog.trees": _page(_CATALOG_TREE),
    "catalog.station_specs": _page(_CATALOG_STATION_SPEC),
    "catalog.roadstop_specs": _page(_CATALOG_ROADSTOP_SPEC),
    "depots": _page(_DEPOT),
    "waypoints": _page(_WAYPOINT),
    "signs": _page(_SIGN),
    "company.appearance": _COMPANY_APPEARANCE,
    "catalog.manager_faces": _MANAGER_FACE_CATALOG,
    "company.report": _COMPANY_REPORT,
    "engine.preview_offers": {**_QUERY_BASE, "items": [_ENGINE_PREVIEW_OFFER],
                              "next_after_id": _NULLABLE_INT},
    "world": _WORLD,
    "land.info": _LAND_INFO,
    "station.site": _STATION_SITE,
    "town.authority": _TOWN_AUTHORITY,
    "town.detail": _TOWN_DETAIL,
    "subsidies": _page(_SUBSIDY),
    "league.performance": {**_QUERY_BASE, "rows": [_LEAGUE_PERFORMANCE_ROW]},
    "league.tables": _page(_LEAGUE_TABLE),
    "league.table.entries": {
        **_QUERY_BASE, "table_id": _HANDLE,
        "title": str, "title_truncated": bool,
        "header": str, "header_truncated": bool,
        "footer": str, "footer_truncated": bool,
        "items": [_LEAGUE_ENTRY], "next_after_id": _NULLABLE_INT,
    },
    "goals": {**_QUERY_BASE, "scope": str, "items": [_GOAL],
              "next_after_id": _NULLABLE_INT},
    "station.details": _STATION_DETAILS,
    "station.cargo.flows": {
        **_QUERY_BASE, "station": _HANDLE, "cargo_id": int,
        "unit": str, "has_cargo_data": bool,
        "distribution_mode": str, "rows": [_STATION_FLOW_ROW],
        "next": {"anyOf": [{"after_source_id": int,
                            "after_share_cumulative": int}, type(None)]},
        "complete": bool,
    },
    "station.cargo.waiting": _STATION_WAITING,
    "station.links": {
        **_QUERY_BASE, "station": _HANDLE, "cargo_id": int,
        "rows": [_STATION_LINK_ROW], "next_after_offset": _NULLABLE_INT,
        "complete": bool,
    },
    "map.region": {**_QUERY_BASE, "map_width": int, "map_height": int,
                   "region": {"x": int, "y": int, "width": int, "height": int},
                   "tiles": [_MAP_TILE], "next_after_offset": _NULLABLE_INT,
                   "complete": bool, "page_tick": int},
    "company": {
        **_QUERY_BASE, "name": str, "balance": int, "loan": int,
        "maximum_loan": int, "loan_interval": int, "income_this_quarter": int,
        "expenses_this_quarter": int,
        "quarter_history": [{"quarter": int, "income": int, "expenses": int, "company_value": int}],
        "infrastructure": {"rail": int, "road": int, "tram": int, "signals": int,
                           "water": int, "station_tiles": int, "airports": int},
        "settings": {"autorenew_enabled": bool, "autorenew_months": int,
                     "autorenew_minimum_money": int, "renew_keep_length": bool,
                     "service_percent": bool, "service_trains": int,
                     "service_road": int, "service_ships": int,
                     "service_aircraft": int},
    },
    "vehicles": {**_QUERY_BASE, "items": [_VEHICLE], "next_after_id": (int, type(None))},
    "stations": {**_QUERY_BASE, "items": [_STATION], "next_after_id": (int, type(None))},
    "groups": {**_QUERY_BASE, "items": [_GROUP], "next_after_id": (int, type(None))},
    "autoreplace": {**_QUERY_BASE, "scope": str, "group_id": _HANDLE,
                          "items": [_AUTOREPLACE_RULE], "next_after_id": (int, type(None))},
    "group.stats": {**_QUERY_BASE, "scope": str, "group_id": _HANDLE,
                    "vehicle_count": int, "profit_this_year": int,
                    "profit_last_year": int, "engine_count": int},
    "orders": _ORDERS_RESULT,
    "timetable": _TIMETABLE_RESULT,
    "catalog.engines": {**_QUERY_BASE, "items": [_ENGINE], "next_after_id": (int, type(None))},
    "catalog.airports": {**_QUERY_BASE, "items": [_AIRPORT]},
    "catalog.cargo": {**_QUERY_BASE, "items": [_CARGO]},
    "catalog.railtypes": {**_QUERY_BASE, "items": [_RAILTYPE]},
    "catalog.roadtypes": {**_QUERY_BASE, "items": [_ROADTYPE]},
    "towns": {**_QUERY_BASE, "items": [_TOWN], "next_after_id": (int, type(None))},
    "industries": {**_QUERY_BASE, "items": [_INDUSTRY], "next_after_id": (int, type(None))},
    "industry.details": _INDUSTRY_DETAILS,
    "industry.monthly_stats": _INDUSTRY_MONTHLY_STATS,
    "display.text": _DISPLAY_TEXT,
}
_LEGACY_QUERY = {**_QUERY_BASE, "vehicles": [_VEHICLE], "stations": [_STATION],
                 "tiles": [_MAP_TILE], "orders": [_ORDER], "error": _ERROR}
_DROP = object()


def _positive(value, shape, depth):
    if depth > 16:
        return _DROP
    if isinstance(shape, dict) and "anyOf" in shape:
        for branch in shape["anyOf"]:
            result = _positive(value, branch, depth + 1)
            if result is not _DROP:
                return result
        return _DROP
    if isinstance(shape, dict) and isinstance(shape.get("type"), str):
        kind = shape["type"]
        if kind == "object":
            shape = shape.get("properties", {})
        elif kind == "array":
            shape = [shape.get("items", {})]
        else:
            shape = {"string": str, "integer": int, "number": (int, float),
                     "boolean": bool, "null": type(None)}.get(kind)
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            return _DROP
        result = {}
        for key, child_shape in shape.items():
            if key in value:
                child = _positive(value[key], child_shape, depth + 1)
                if child is not _DROP:
                    result[key] = child
        return result
    if isinstance(shape, list):
        if not isinstance(value, list) or len(shape) != 1:
            return _DROP
        result = []
        for item in value[:4096]:
            child = _positive(item, shape[0], depth + 1)
            if child is not _DROP:
                result.append(child)
        return result
    if isinstance(shape, (type, tuple)):
        allowed = shape if isinstance(shape, tuple) else (shape,)
        if any(type(value) is kind for kind in allowed):
            return value
    return _DROP


def positive(value, shape, depth=0):
    """Project known fields recursively; unknown or ill-typed fields disappear."""
    result = _positive(value, shape, depth)
    return None if result is _DROP else result


def _fields(spec, vocabulary):
    return {key: vocabulary[key] for key in spec.get("result_fields", ()) if key in vocabulary}


def _bounded_feedback(value, maximum):
    text = "".join(char for char in value
                   if char in "\n\t" or not unicodedata.category(char).startswith("C"))
    return text.encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


def outcome(value, spec=None, *, visual=False):
    vocabulary = _VISUAL_OUTCOME if visual else _OUTCOME
    if isinstance(spec, dict) and "result_fields" in spec:
        result = positive(value, _fields(spec, vocabulary))
    else:
        result = positive(value, vocabulary if visual else spec or vocabulary)
    if visual and isinstance(result, dict) and "error" in result:
        result["error"] = error(result["error"], visual=True)
    if visual and isinstance(result, dict):
        for key, pattern in (("order_version", _ORDER_VERSION),
                             ("source_order_version", _ORDER_VERSION),
                             ("page_version", _PAGE_VERSION)):
            if key in result and pattern.fullmatch(result[key]) is None:
                result.pop(key)
    return result


def control_reference(value, requested_kind, engine_instance_id):
    """Return actuator identities and opaque fences, never target attributes."""
    def unavailable():
        raise ServiceError("control_reference_unavailable", status=409)

    try:
        if len(dumps(value).encode("utf-8")) > 8192:
            unavailable()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        unavailable()
    if (not isinstance(requested_kind, str) or not isinstance(engine_instance_id, str) or
            not isinstance(value, dict) or value.get("kind") != requested_kind):
        unavailable()
    if requested_kind == "engine":
        engine_id = value.get("engine_id")
        if type(engine_id) is not int or not 0 <= engine_id <= 65534:
            unavailable()
        return {"kind": requested_kind, "engine_id": engine_id}
    if requested_kind not in _CONTROL_REFERENCE_IDS:
        unavailable()

    def handle(raw, maximum):
        if (not isinstance(raw, dict) or
                type(raw.get("id")) is not int or not 0 <= raw["id"] <= maximum or
                type(raw.get("generation")) is not int or not 1 <= raw["generation"] <= 0xFFFFFFFFFFFFFFFF or
                not isinstance(raw.get("engine_instance_id"), str) or
                not 1 <= len(raw["engine_instance_id"].encode("utf-8")) <= 256 or
                raw["engine_instance_id"] != engine_instance_id):
            unavailable()
        return {key: raw[key] for key in _HANDLE}

    result = {"kind": requested_kind,
              "reference": handle(value.get("reference"), _CONTROL_REFERENCE_IDS[requested_kind])}
    if requested_kind == "vehicle" and "order_version" in value:
        version = value["order_version"]
        if not isinstance(version, str) or _ORDER_VERSION.fullmatch(version) is None:
            unavailable()
        result["order_version"] = version
    if requested_kind in ("story_page", "story_element"):
        page = handle(value.get("page"), 63999)
        version = value.get("page_version")
        if (not isinstance(version, str) or _PAGE_VERSION.fullmatch(version) is None or
                requested_kind == "story_page" and page != result["reference"]):
            unavailable()
        result.update(page=page, page_version=version)
    return result


def estimate(value):
    """Project one completed native test result without world-state fields."""
    def unavailable():
        raise ServiceError("engine_unavailable", status=503)

    try:
        if len(dumps(value).encode("utf-8")) > 4096:
            unavailable()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        unavailable()
    if (not isinstance(value, dict) or type(value.get("success")) is not bool or
            type(value.get("cost")) is not int or not -(1 << 63) <= value["cost"] < (1 << 63) or
            value.get("advisory") is not True or
            type(value.get("test_and_execute_may_differ")) is not bool or
            ("error" in value) == value["success"]):
        unavailable()
    result = {key: value[key] for key in ("success", "cost", "advisory",
                                         "test_and_execute_may_differ")}
    if not value["success"]:
        native_error = value["error"]
        if (not isinstance(native_error, dict) or
                native_error.get("code") != "gameplay_rejected" or
                not isinstance(native_error.get("message"), str)):
            unavailable()
        try:
            message = _bounded_feedback(native_error["message"], 1024)
            if len(native_error["message"].encode("utf-8")) > 1024:
                unavailable()
        except (TypeError, ValueError, UnicodeError):
            unavailable()
        result["error"] = {"code": "gameplay_rejected", "message": message}
    try:
        if len(dumps(result).encode("utf-8")) > 4096:
            unavailable()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        unavailable()
    return result


def observation(value, spec=None, query_name=None, *, parameters=None):
    if isinstance(spec, str) and query_name is None:
        query_name, spec = spec, None
    if query_name is None and isinstance(spec, dict):
        query_name = spec.get("name")
    if spec is None and query_name:
        spec = query_spec(query_name)
    if isinstance(spec, dict) and "result_fields" in spec:
        vocabulary = _QUERY_FIELDS.get(query_name, {})
        fields = _fields(spec, vocabulary)
        if query_name == "industry.monthly_stats" and "points" in fields:
            direction = value.get("direction") if isinstance(value, dict) else None
            point = (_INDUSTRY_ACCEPTED_POINT if direction == "accepted" else
                     _INDUSTRY_PRODUCED_POINT if direction == "produced" else
                     {"periods_ago": int})
            fields["points"] = [point]
        if query_name == "display.text":
            kind = value.get("kind") if isinstance(value, dict) else None
            if kind == "cargo_name":
                references = {"cargo_id"}
            elif isinstance(kind, str) and kind in _DISPLAY_TEXT_TARGET_KINDS:
                references = {"target"}
            elif kind == "vehicle_refit_label":
                references = {"target", "vehicle_id", "cargo_id", "subtype"}
            elif kind == "industry_cargo_line":
                references = {"target", "cargo_id", "direction", "slot"}
            else:
                fields = {key: shape for key, shape in fields.items()
                          if key in _QUERY_BASE}
                references = set()
            for key in _DISPLAY_TEXT_REFERENCE_FIELDS - references:
                fields.pop(key, None)
        result = positive(value, fields)
        if query_name == "vehicle.refit_subtypes" and isinstance(result, dict):
            subtypes = result.get("subtypes")
            if isinstance(subtypes, list):
                result["subtypes"] = subtypes[:16]
        if (query_name in ("orders", "timetable") and isinstance(result, dict) and
                not (isinstance(parameters, dict) and parameters.get("include_live_detail") is True)):
            result.pop("current_execution", None)
            for order in result.get("orders", ()):
                order.pop("destination_entity", None)
        if query_name == "vehicle.details" and isinstance(result, dict) and result.get("owned_by_actor") is not True:
            result.pop("can_refit_now", None)
            for part in result.get("parts", ()):
                part.pop("refittable_cargo_ids", None)
        return result
    return positive(value, spec or _LEGACY_QUERY)


def error(value, *, visual=False):
    if not visual:
        return positive(value, _ERROR)
    result = positive(value, {"code": str, "message": str, "game_message": str})
    if isinstance(result, dict):
        for key in result:
            result[key] = _bounded_feedback(result[key], 256 if key == "code" else 2048)
    return result


def chat(value):
    return positive(value, _CHAT)
