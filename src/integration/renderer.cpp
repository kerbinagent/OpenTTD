/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file renderer.cpp Read-only, private native rendering worker. */

#include "../stdafx.h"
#include "renderer.h"
#include "renderer_image.h"
#include "renderer_observation.h"
#include "renderer_input.h"
#include "actions.h"
#include "integration_bridge.h"
#include "visual_commands.h"
#include "../command_func.h"
#include "../blitter/factory.hpp"
#include "../company_base.h"
#include "../company_func.h"
#include "../company_gui.h"
#include "../core/backup_type.hpp"
#include "../fileio_type.h"
#include "../gfx_func.h"
#include "../gui.h"
#include "../landscape.h"
#include "../network/network.h"
#include "../network/network_client.h"
#include "../network/network_base.h"
#include "../network/network_func.h"
#include "../network/network_internal.h"
#include "../rail.h"
#include "../rail_gui.h"
#include "../road.h"
#include "../road_func.h"
#include "../road_gui.h"
#include "../terraform_gui.h"
#include "../tile_cmd.h"
#include "../toolbar_gui.h"
#include "../timer/timer_game_tick.h"
#include "../vehicle_base.h"
#include "../vehicle_func.h"
#include "../vehicle_gui.h"
#include "../viewport_func.h"
#include "../window_func.h"
#include "../window_gui.h"
#include "../zoom_func.h"
#include <cstdlib>
#include <atomic>
#include <charconv>
#include <deque>
#include <filesystem>
#include <random>
#include <unordered_map>
#include <unordered_set>

#include "../strings_func.h"
#include "../string_func.h"
#include "../error.h"
#include "table/strings.h"

#include "../safeguards.h"

namespace Integration {
namespace {

constexpr size_t MAX_RETAINED_IMAGES = 4;
constexpr int MAX_CAPTURE_WIDTH = 2560;
constexpr int MAX_CAPTURE_HEIGHT = 1440;
constexpr size_t MAX_CAPTURED_BUNDLES = 32;
constexpr size_t MAX_OBSERVATIONS = 64;
constexpr auto OBSERVATION_RETENTION = std::chrono::seconds(60);
constexpr auto OBSERVATION_INTERVAL = std::chrono::milliseconds(100);
constexpr int64_t OBSERVATION_COORDINATE_LIMIT = 1LL << 28;

using TileSignature = std::array<uint8_t, 12>;

struct WindowSnapshot {
	const Window *identity;
	WindowClass window_class;
	WindowNumber number;
	int left, top, width, height;
};

struct ScreenshotSnapshot {
	std::string id;
	uint64_t lifecycle_revision;
	std::chrono::steady_clock::time_point created;
	int width = 0;
	int height = 0;
	Viewport viewport{};
	std::vector<WindowSnapshot> windows;
	uint32_t frame = 0;
};

/** Observation evidence never becomes a native UI/input snapshot. */
struct ObservationSnapshot {
	std::string request;
	std::string payload;
	uint64_t lifecycle_revision;
	std::chrono::steady_clock::time_point created;
	Json metadata;
	std::filesystem::path artifact;
	std::optional<Viewport> viewport;
};

struct RendererState {
	CompanyID company = COMPANY_SPECTATOR;
	std::string session;
	std::string directory;
	std::deque<std::filesystem::path> artifacts;
	std::chrono::steady_clock::time_point last_capture{};
	std::chrono::steady_clock::time_point control_interval{};
	size_t controls_in_interval = 0;
	std::unique_ptr<ScreenshotSnapshot> screenshot;
	Json image_metadata;
	std::deque<ObservationSnapshot> observations;
	std::chrono::steady_clock::time_point last_observation{};
	int observation_gui_scale = 0;
	ZoomLevel observation_font_zoom{};
	bool observation_population_in_label = false;
	bool observation_rendered_in_tick = false;
	bool parked = false;
	uint32_t parked_frame = 0;
	uint64_t park_revision = 0;
	std::chrono::steady_clock::time_point park_deadline{};
	std::string authority_instance;
	std::string world_epoch;
	uint32_t authority_max_lag_ticks = 0;
	std::string active_request;
	uint64_t input_revision = 0;
	bool capturing = false;
	bool text_only = false;
	bool capture_failed = false;
	bool replay_context = false;
	Json building_bundle;
	std::unordered_map<std::string, Json> bundles;
	std::unordered_map<std::string, std::string> payloads;
	std::deque<std::string> bundle_order;
	std::unordered_set<std::string> callback_failures;
	std::unordered_map<std::string, std::string> phases;
};

RendererState _renderer;
std::atomic<uint64_t> _lifecycle_revision{0};

Json Failure(const char *code)
{
	/* No map state, object identity, filesystem path or gameplay detail in failures. */
	return {{"error", {{"code", code}, {"message", "Rendering request unavailable"}}}};
}

void EndParking()
{
	RendererClearAccessSnapshot();
	_renderer.parked = false;
	_renderer.screenshot.reset();
	_renderer.image_metadata = Json{};
}

uint32_t ParkDurationMs()
{
	uint32_t duration = 8000;
	if (const char *configured = std::getenv("OPENTTD_RENDERER_PARK_MS")) {
		uint32_t parsed = 0;
		auto [end, error] = std::from_chars(configured, configured + strlen(configured), parsed);
		if (error == std::errc{} && *end == '\0') duration = parsed;
	}
	/* Leave at least two seconds for catch-up and native acknowledgements. */
	const uint32_t existing_lag = _frame_counter_server > _frame_counter ? _frame_counter_server - _frame_counter : 0;
	const uint64_t lag_ms = _renderer.authority_max_lag_ticks > existing_lag ? static_cast<uint64_t>(_renderer.authority_max_lag_ticks - existing_lag) * MILLISECONDS_PER_TICK : 0;
	return static_cast<uint32_t>(std::min<uint64_t>(std::clamp(duration, 100u, MAX_RENDERER_PARK_MS), lag_ms > 2000 ? lag_ms - 2000 : 0));
}

Json BundleBase(const std::string &request, const std::string &image, uint32_t frame)
{
	/* Bind the proof to the connection identity reported by the authoritative server. */
	const NetworkClientInfo *client = NetworkClientInfo::GetByClientID(_network_own_client_id);
	if (client == nullptr || client->public_key.empty()) throw std::runtime_error("Rendering owner unavailable");
	return {{"version", 2}, {"request_id", request}, {"image_id", image}, {"source_frame", frame},
		{"authority_instance_id", _renderer.authority_instance}, {"world_epoch", _renderer.world_epoch}, {"worker_instance_id", InstanceId()},
		{"owner", {{"client_id", to_underlying(_network_own_client_id)}, {"public_key", client->public_key}}},
		{"commands", Json::array()}, {"tile_proof", nullptr}};
}

void RememberBundle(const Json &bundle, const std::string &payload)
{
	const std::string request = bundle.at("request_id").get<std::string>();
	_renderer.bundles.emplace(request, bundle);
	_renderer.payloads.emplace(request, payload);
	_renderer.bundle_order.push_back(request);
	_renderer.phases.emplace(request, "captured");
}

void RenderRejected()
{
	if (_game_mode != GameMode::Normal || GetMainWindow() == nullptr || _local_company != _renderer.company) return;
	AutoRestoreBackup company_scope(_current_company, _renderer.company);
	ShowErrorMessage(GetEncodedString(STR_ERROR_CAN_T_DO_THIS), {}, WarningLevel::Error);
}

std::string OpaqueId()
{
	std::random_device random;
	return fmt::format("{:08x}{:08x}{:08x}{:08x}", random(), random(), random(), random());
}

bool Ready()
{
	if (!RendererIsActive() || _renderer.session.empty() || _game_mode != GameMode::Normal || !_networking || _network_server || !MyClient::IsConnected() || _network_first_time) return false;
	if (!_renderer.parked && _frame_counter_server > _frame_counter && _frame_counter_server - _frame_counter > Ticks::DAY_TICKS) return false;
	if (_renderer.company != COMPANY_SPECTATOR && !Company::IsValidID(_renderer.company)) return false;
	if (GetMainWindow() == nullptr || GetMainWindow()->viewport == nullptr || _screen.dst_ptr == nullptr) return false;
	if (BlitterFactory::GetCurrentBlitter()->GetScreenDepth() != 32) return false;
	/* A server-enforced company move must never be undone by observation IPC. */
	if (_local_company != _renderer.company) return false;
	return true;
}

TileIndex TileAtPixel(const Viewport &viewport, int x, int y)
{
	Point point = TranslateXYToTileCoord(viewport, x, y, false);
	if (point.x < 0 || point.y < 0 || point.x >= static_cast<int>(Map::SizeX() * TILE_SIZE) || point.y >= static_cast<int>(Map::SizeY() * TILE_SIZE)) return INVALID_TILE;
	TileIndex tile = TileVirtXY(point.x, point.y);
	return IsValidTile(tile) ? tile : INVALID_TILE;
}

std::vector<WindowSnapshot> WindowLayout()
{
	std::vector<WindowSnapshot> result;
	for (const Window *window : Window::IterateFromFront()) {
		result.push_back({window, window->window_class, window->window_number, window->left, window->top, window->width, window->height});
	}
	return result;
}

bool SameLayout(const ScreenshotSnapshot &screenshot)
{
	auto layout = WindowLayout();
	if (layout.size() != screenshot.windows.size()) return false;
	for (size_t i = 0; i < layout.size(); ++i) {
		const auto &a = layout[i];
		const auto &b = screenshot.windows[i];
		if (a.identity != b.identity || a.window_class != b.window_class || a.number != b.number || a.left != b.left || a.top != b.top || a.width != b.width || a.height != b.height) return false;
	}
	const Viewport &viewport = *GetMainWindow()->viewport;
	const Viewport &previous = screenshot.viewport;
	return viewport.left == previous.left && viewport.top == previous.top && viewport.width == previous.width && viewport.height == previous.height &&
		viewport.virtual_left == previous.virtual_left && viewport.virtual_top == previous.virtual_top && viewport.zoom == previous.zoom;
}

std::unique_ptr<ScreenshotSnapshot> Snapshot(const std::string &id)
{
	auto snapshot = std::make_unique<ScreenshotSnapshot>();
	snapshot->id = id;
	snapshot->lifecycle_revision = _lifecycle_revision.load();
	snapshot->created = std::chrono::steady_clock::now();
	snapshot->width = _screen.width;
	snapshot->height = _screen.height;
	snapshot->viewport = *GetMainWindow()->viewport;
	snapshot->windows = WindowLayout();
	snapshot->frame = _frame_counter;
	return snapshot;
}

Json Inspect(const Json &params)
{
	if (!_renderer.screenshot || !params.contains("screenshot_id") || params.at("screenshot_id") != _renderer.screenshot->id) return Failure("stale_view");
	const ScreenshotSnapshot &snapshot = *_renderer.screenshot;
	if (snapshot.lifecycle_revision != _lifecycle_revision.load() || std::chrono::steady_clock::now() - snapshot.created > std::chrono::milliseconds(MAX_RENDERER_PARK_MS) || !SameLayout(snapshot)) return Failure("stale_view");
	const int x = params.at("x").get<int>();
	const int y = params.at("y").get<int>();
	if (!RendererShouldPark() || snapshot.frame != _frame_counter || x < 0 || y < 0 || x >= snapshot.width || y >= snapshot.height) return Failure("stale_view");
	Window *window = FindWindowFromPt(x, y);
	if (window != GetMainWindow() || IsPtInWindowViewport(window, x, y) == nullptr) return Failure("invalid_target");
	const Vehicle *vehicle = CheckClickOnVehicle(*window->viewport, x, y);
	if (vehicle != nullptr && IsCompanyBuildableVehicleType(vehicle)) {
		ShowVehicleViewWindow(vehicle->First());
	} else {
		const TileIndex tile = TileAtPixel(*window->viewport, x, y);
		if (tile == INVALID_TILE) return Failure("invalid_target");
		if (!ClickTile(tile)) ShowLandInfo(tile);
	}
	EndParking();
	return {{"status", "ok"}};
}

bool PrepareDirectory()
{
	if (_renderer.directory.empty()) return false;
	std::error_code error;
	std::filesystem::path directory(_renderer.directory);
	if (!directory.is_absolute()) return false;
	const bool created = std::filesystem::create_directories(directory, error);
	if (error || std::filesystem::is_symlink(directory, error) || !std::filesystem::is_directory(directory, error)) return false;
	if (created) std::filesystem::permissions(directory, std::filesystem::perms::owner_all, std::filesystem::perm_options::replace, error);
	if (error) return false;
	/* Do not change permissions on an operator-supplied existing shared directory. */
	const auto permissions = std::filesystem::status(directory, error).permissions();
	return !error && (permissions & (std::filesystem::perms::group_all | std::filesystem::perms::others_all)) == std::filesystem::perms::none;
}

void PruneObservations()
{
	const auto now = std::chrono::steady_clock::now();
	while (!_renderer.observations.empty() && now - _renderer.observations.front().created >= OBSERVATION_RETENTION) {
		std::error_code error;
		if (!_renderer.observations.front().artifact.empty()) std::filesystem::remove(_renderer.observations.front().artifact, error);
		/* Retain a slot if its file cannot be removed; admitting replacement files
		 * despite an unlink failure would lose the disk-space bound. */
		if (error) break;
		_renderer.observations.pop_front();
	}
}

void ObservationKeys(const Json &value, std::initializer_list<std::string_view> required, std::initializer_list<std::string_view> optional = {})
{
	if (!value.is_object()) throw std::invalid_argument("Observation object required");
	for (auto key : required) if (!value.contains(std::string(key))) throw std::invalid_argument("Observation field missing");
	for (const auto &[key, child] : value.items()) {
		if (std::find(required.begin(), required.end(), key) == required.end() && std::find(optional.begin(), optional.end(), key) == optional.end()) {
			throw std::invalid_argument("Unknown observation field");
		}
	}
}

uint64_t ObservationInteger(const Json &value, uint64_t maximum)
{
	if (!value.is_number_integer()) throw std::invalid_argument("Observation integer required");
	if (value.is_number_unsigned()) {
		const uint64_t number = value.get<uint64_t>();
		if (number <= maximum) return number;
	} else {
		const int64_t number = value.get<int64_t>();
		if (number >= 0 && static_cast<uint64_t>(number) <= maximum) return number;
	}
	throw std::invalid_argument("Observation integer out of bounds");
}

ZoomLevel ObservationZoom(const Json &value)
{
	static constexpr std::pair<std::string_view, ZoomLevel> levels[] = {
		{"in4x", ZoomLevel::In4x}, {"in2x", ZoomLevel::In2x}, {"normal", ZoomLevel::Normal},
		{"out2x", ZoomLevel::Out2x}, {"out4x", ZoomLevel::Out4x}, {"out8x", ZoomLevel::Out8x},
	};
	if (value.is_string()) for (const auto &[name, zoom] : levels) {
		if (value == name && zoom >= _settings_client.gui.zoom_min && zoom <= _settings_client.gui.zoom_max) return zoom;
	}
	throw std::invalid_argument("Invalid observation zoom");
}

ObservationLabels ReadObservationLabels(const Json &value)
{
	ObservationKeys(value, {"town_names", "station_names", "waypoint_names", "signs", "competitor_labels"});
	for (const auto &child : value) if (!child.is_boolean()) throw std::invalid_argument("Observation label boolean required");
	return {value.at("town_names").get<bool>(), value.at("station_names").get<bool>(), value.at("waypoint_names").get<bool>(),
		value.at("signs").get<bool>(), value.at("competitor_labels").get<bool>()};
}

struct ExpiredObservation : std::exception {};

int ObservationCoordinate(int64_t value)
{
	if (value < -OBSERVATION_COORDINATE_LIMIT || value > OBSERVATION_COORDINATE_LIMIT) throw std::invalid_argument("Observation coordinate out of bounds");
	return static_cast<int>(value);
}

Viewport ObservationViewport(const Json &params)
{
	const auto &output = params.at("output");
	ObservationKeys(output, {"width", "height"});
	Viewport viewport{};
	viewport.width = static_cast<int>(ObservationInteger(output.at("width"), MAX_OBSERVATION_WIDTH));
	viewport.height = static_cast<int>(ObservationInteger(output.at("height"), MAX_OBSERVATION_HEIGHT));
	if (viewport.width < 64 || viewport.height < 64 || static_cast<uint64_t>(viewport.width) * viewport.height > MAX_OBSERVATION_PIXELS) throw std::invalid_argument("Invalid observation dimensions");
	const auto &selection = params.at("selection");
	if (!selection.is_object()) throw std::invalid_argument("Observation selection required");
	viewport.zoom = ObservationZoom(selection.at("zoom_level"));
	viewport.virtual_width = ScaleByZoom(viewport.width, viewport.zoom);
	viewport.virtual_height = ScaleByZoom(viewport.height, viewport.zoom);
	if (static_cast<uint64_t>(viewport.virtual_width) * viewport.virtual_height > MAX_OBSERVATION_VIRTUAL_AREA) throw std::invalid_argument("Observation coverage exceeds limits");
	Point center;
	if (selection.at("kind") == "map_center") {
		ObservationKeys(selection, {"kind", "tile", "zoom_level"});
		const auto &tile = selection.at("tile");
		ObservationKeys(tile, {"x", "y"});
		const uint32_t x = static_cast<uint32_t>(ObservationInteger(tile.at("x"), Map::SizeX() - 1));
		const uint32_t y = static_cast<uint32_t>(ObservationInteger(tile.at("y"), Map::SizeY() - 1));
		if (!IsValidTile(TileXY(x, y))) throw std::invalid_argument("Invalid observation center");
		const int world_x = x * TILE_SIZE + TILE_SIZE / 2;
		const int world_y = y * TILE_SIZE + TILE_SIZE / 2;
		center = RemapCoords(world_x, world_y, GetSlopePixelZ(world_x, world_y));
	} else if (selection.at("kind") == "map_image_point") {
		ObservationKeys(selection, {"kind", "image_id", "pixel", "zoom_level"});
		const auto &pixel = selection.at("pixel");
		ObservationKeys(pixel, {"x", "y"});
		const auto found = std::find_if(_renderer.observations.begin(), _renderer.observations.end(), [&](const ObservationSnapshot &source) {
			return source.viewport.has_value() && source.metadata.is_object() && source.metadata.value("image_id", Json()) == selection.at("image_id");
		});
		if (found == _renderer.observations.end() || found->lifecycle_revision != _lifecycle_revision.load() ||
				std::chrono::steady_clock::now() - found->created >= OBSERVATION_RETENTION) throw ExpiredObservation();
		const Viewport &source = *found->viewport;
		const int x = static_cast<int>(ObservationInteger(pixel.at("x"), source.width - 1));
		const int y = static_cast<int>(ObservationInteger(pixel.at("y"), source.height - 1));
		/* Preserve the chosen projected image point. Never identify a tile or entity. */
		center = {ObservationCoordinate(static_cast<int64_t>(source.virtual_left) + ScaleByZoom(x, source.zoom)),
			ObservationCoordinate(static_cast<int64_t>(source.virtual_top) + ScaleByZoom(y, source.zoom))};
	} else throw std::invalid_argument("Invalid observation selection");
	const int mask = -ScaleByZoom(1, viewport.zoom);
	viewport.virtual_left = ObservationCoordinate(static_cast<int64_t>(center.x) - viewport.virtual_width / 2) & mask;
	viewport.virtual_top = ObservationCoordinate(static_cast<int64_t>(center.y) - viewport.virtual_height / 2) & mask;
	ObservationCoordinate(static_cast<int64_t>(viewport.virtual_left) + viewport.virtual_width);
	ObservationCoordinate(static_cast<int64_t>(viewport.virtual_top) + viewport.virtual_height);
	return viewport;
}

Json PublishObservation(ObservationSnapshot &snapshot, ObservationPixels pixels, std::optional<Viewport> viewport, const char *kind)
{
	/* A previous failed publication may have copied a file before metadata allocation failed. */
	if (!snapshot.artifact.empty()) {
		std::error_code error;
		std::filesystem::remove(snapshot.artifact, error);
		snapshot.artifact.clear();
	}
	RendererImage image = EncodeRendererImage(pixels.rgb, pixels.width, pixels.height);
	const std::string image_id = OpaqueId();
	const std::filesystem::path path = std::filesystem::path(_renderer.directory) / ("observation-" + image_id + ".png");
	auto file = FileHandle::Open(path.string(), "wbx");
	if (!file.has_value()) return Failure("capture_unavailable");
	const bool written = fwrite(image.bytes.data(), 1, image.bytes.size(), *file) == image.bytes.size() && fflush(*file) == 0;
	file->Close();
	std::error_code error;
	if (written) std::filesystem::permissions(path, std::filesystem::perms::owner_read | std::filesystem::perms::owner_write, std::filesystem::perm_options::replace, error);
	if (!written || error) { std::filesystem::remove(path, error); return Failure("capture_unavailable"); }
	/* Publish only after the entire immutable artifact is available. */
	snapshot.artifact = path;
	snapshot.viewport = std::move(viewport);
	snapshot.metadata = {{"image_id", image_id}, {"path", path.string()}, {"width", pixels.width}, {"height", pixels.height},
		{"content_type", image.content_type}, {"capture_kind", kind}, {"source_frame", _frame_counter},
		{"authority_instance_id", _renderer.authority_instance}, {"world_epoch", _renderer.world_epoch}, {"worker_instance_id", InstanceId()},
		{"renderer_lifecycle_revision", snapshot.lifecycle_revision}};
	return snapshot.metadata;
}

Json Observe(const Json &params, bool world)
{
	try {
		if (world) ObservationKeys(params, {"observation_request_id", "authority_instance_id", "selection", "output", "labels"}, {"minimum_source_frame"});
		else ObservationKeys(params, {"observation_request_id", "authority_instance_id", "panel", "presentation", "region"}, {"minimum_source_frame"});
		const auto &request = params.at("observation_request_id");
		if (!request.is_string()) return Failure("invalid_request");
		const auto &id = request.get_ref<const std::string &>();
		if (id.size() != 32 || !std::all_of(id.begin(), id.end(), [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); })) return Failure("invalid_request");
		PruneObservations();
		if (!Ready()) return Failure("renderer_not_ready");
		if (_renderer.authority_instance.empty() || params.at("authority_instance_id") != _renderer.authority_instance) return Failure("observation_expired");
#ifndef WITH_PNG
		return Failure("png_required");
#else
		const std::string payload = Json{{"kind", world ? "world" : "native_info"}, {"parameters", params}}.dump();
		auto found = std::find_if(_renderer.observations.begin(), _renderer.observations.end(), [&](const ObservationSnapshot &snapshot) { return snapshot.request == id; });
		if (found != _renderer.observations.end()) {
			if (found->payload != payload) return Failure("invalid_request");
			if (found->lifecycle_revision != _lifecycle_revision.load() ||
					std::chrono::steady_clock::now() - found->created >= OBSERVATION_RETENTION) return Failure("observation_expired");
			if (found->metadata.is_object()) return found->metadata;
		}
		const uint32_t minimum_frame = params.contains("minimum_source_frame") ? static_cast<uint32_t>(ObservationInteger(params.at("minimum_source_frame"), UINT32_MAX)) : 0;
		std::optional<Viewport> viewport;
		ObservationLabels labels{};
		bool compact = false;
		uint8_t criterion = 0;
		bool descending = false;
		uint32_t first_row = 0;
		if (world) {
			labels = ReadObservationLabels(params.at("labels"));
			viewport = ObservationViewport(params);
		} else {
			if (params.at("region") != "window") return Failure("invalid_request");
			const auto &presentation = params.at("presentation");
			if (params.at("panel") == "company_finances") {
				ObservationKeys(presentation, {"layout"});
				if (presentation.at("layout") != "compact" && presentation.at("layout") != "expanded") return Failure("invalid_request");
				compact = presentation.at("layout") == "compact";
			} else if (params.at("panel") == "town_directory") {
				ObservationKeys(presentation, {"sort", "scroll"});
				const auto &sort = presentation.at("sort");
				ObservationKeys(sort, {"by", "direction"});
				if (sort.at("by") == "name") criterion = 0;
				else if (sort.at("by") == "population") criterion = 1;
				else if (sort.at("by") == "rating") criterion = 2;
				else return Failure("invalid_request");
				if (sort.at("direction") != "ascending" && sort.at("direction") != "descending") return Failure("invalid_request");
				descending = sort.at("direction") == "descending";
				ObservationKeys(presentation.at("scroll"), {"first_row"});
				first_row = static_cast<uint32_t>(ObservationInteger(presentation.at("scroll").at("first_row"), UINT32_MAX));
			} else return Failure("invalid_request");
		}
		if (found == _renderer.observations.end()) {
			if (_renderer.observations.size() >= MAX_OBSERVATIONS) return Failure("rate_limited");
			_renderer.observations.push_back({id, payload, _lifecycle_revision.load(), std::chrono::steady_clock::now(), Json(), {}, {}});
			found = std::prev(_renderer.observations.end());
		}
		/* IPC runs at the settled beginning of GameLoop. Never wait here for replay:
		 * returning lets the coordinator retry while NetworkGameLoop advances. */
		if (_frame_counter < minimum_frame) return Failure("replica_pending");
		if (!world && (!_renderer.active_request.empty() || RendererShouldPark())) return Failure("renderer_busy");
		/* Never make a temporary population-label change: cached town-sign geometry would
		 * become inconsistent. The configured worker's font/format profile is immutable. */
		if (_renderer.observation_gui_scale != _gui_scale || _renderer.observation_font_zoom != _font_zoom ||
				_renderer.observation_population_in_label != _settings_client.gui.population_in_label) return Failure("capture_unavailable");
		const auto now = std::chrono::steady_clock::now();
		if (_renderer.observation_rendered_in_tick || now - _renderer.last_observation < OBSERVATION_INTERVAL) return Failure("rate_limited");
		if (!PrepareDirectory()) return Failure("capture_unavailable");
		_renderer.last_observation = now;
		_renderer.observation_rendered_in_tick = true;
		AutoRestoreBackup company_scope(_current_company, _renderer.company);
		if (world) return PublishObservation(*found, DrawWorldObservation(*viewport, labels), viewport, "world");
		Window *window = params.at("panel") == "company_finances" ?
			PrepareCompanyFinancesWindowForCapture(_renderer.company, compact) : PrepareTownDirectoryWindowForCapture(criterion, descending, first_row);
		if (window == nullptr) return Failure("capture_unavailable");
		return PublishObservation(*found, DrawNativePanelObservation(*window), {}, "native_info");
#endif
	} catch (const ExpiredObservation &) { return Failure("observation_expired"); }
	catch (const std::invalid_argument &) { return Failure("invalid_request"); }
	catch (const Json::exception &) { return Failure("invalid_request"); }
	catch (const std::exception &) { return Failure("capture_unavailable"); }
}

} // namespace

bool RendererIsActive()
{
	/* Startup role, not the later IPC configuration, activates every command guard. */
	const char *role = std::getenv("OPENTTD_INTEGRATION_ROLE");
	return role != nullptr && std::string_view(role) == "renderer";
}

bool RendererShouldPark()
{
	if (!RendererIsActive() || !_renderer.parked) return false;
	if (!MyClient::IsConnected() || !_networking || _network_server || _frame_counter != _renderer.parked_frame ||
		std::chrono::steady_clock::now() >= _renderer.park_deadline ||
		(_frame_counter_server > _frame_counter && _frame_counter_server - _frame_counter + Ticks::DAY_TICKS >= _renderer.authority_max_lag_ticks) ||
		_renderer.park_revision != _lifecycle_revision.load()) {
		EndParking();
		return false;
	}
	return true;
}

void RendererTick()
{
	if (RendererIsActive()) {
		RendererShouldPark();
		PruneObservations();
	}
}

Json RendererObserveWorld(const Json &params) { return Observe(params, true); }
Json RendererObserveNativeInfo(const Json &params) { return Observe(params, false); }
void RendererBeginObservationTick() { _renderer.observation_rendered_in_tick = false; }

void RendererInvalidateSnapshot()
{
	/* No state access: safe from lifecycle callbacks during world teardown. */
	if (RendererIsActive()) ++_lifecycle_revision;
}

void ConfigureRenderer(CompanyID company, const std::string &session, const std::string &authority_instance, const std::string &world_epoch, uint32_t authority_max_lag_ticks)
{
	if (!RendererIsActive() || session.empty() || session.size() > 128) throw std::invalid_argument("Invalid rendering configuration");
	/* Title-game companies must never become immutable bindings for a joining replica. */
	if (_game_mode != GameMode::Normal || !_networking || _network_server || !MyClient::IsConnected() || _network_first_time || _local_company != company) throw std::invalid_argument("Rendering replica is not ready");
	if (!authority_instance.empty() && !world_epoch.empty()) {
		if (!_renderer.authority_instance.empty() && (_renderer.authority_instance != authority_instance || _renderer.world_epoch != world_epoch)) throw std::invalid_argument("Rendering authority binding is immutable");
		_renderer.authority_instance = authority_instance;
		_renderer.world_epoch = world_epoch;
	}
	if (authority_max_lag_ticks != 0) {
		if (_renderer.authority_max_lag_ticks != 0 && _renderer.authority_max_lag_ticks != authority_max_lag_ticks) throw std::invalid_argument("Rendering lag binding is immutable");
		_renderer.authority_max_lag_ticks = authority_max_lag_ticks;
	}
	if (_renderer.company == company && _renderer.session == session) return;
	if (!_renderer.session.empty()) throw std::invalid_argument("Rendering worker binding is immutable");
	_renderer.company = company;
	_renderer.session = session;
	_renderer.observation_gui_scale = _gui_scale;
	_renderer.observation_font_zoom = _font_zoom;
	_renderer.observation_population_in_label = _settings_client.gui.population_in_label;
	_settings_client.gui.auto_scrolling = ViewportAutoscrolling::Disabled;
	_renderer.screenshot.reset();
	const char *directory = std::getenv("OPENTTD_RENDERER_IMAGE_DIR");
	if (directory == nullptr) directory = std::getenv("OPENTTD_INTEGRATION_IMAGE_DIR");
	_renderer.directory = directory == nullptr ? std::string{} : directory;
}

bool RendererCaptureCommandIntent(Commands, StringID, CompanyID, const CommandDataBuffer &)
{
	/* Direct gameplay sends are blocked; only captured native UI intents may be forwarded. */
	return RendererIsActive();
}

bool RendererCaptureNativeCommand(Commands command, StringID error, CommandCallback *callback, CompanyID company, const CommandDataBuffer &data)
{
	if (!RendererIsActive()) return false;
	/* Native UI effects remain; automatic callback construction needs a fresh gesture. */
	if (_renderer.replay_context) { _renderer.capture_failed = true; return false; }
	if (!_renderer.capturing) return false;
	try {
		if (_renderer.text_only) throw std::runtime_error("blocked");
		if (company != _renderer.company || data.size() > 8192 || !IsAllowedNativeCommand(command)) throw std::runtime_error("blocked");
		if (_pause_mode.Any() && !IsCommandAllowedWhilePaused(command)) throw std::runtime_error("blocked");
		auto callback_index = NativeCallbackIndex(command, callback);
		CommandDataBuffer canonical = data;
		if (!callback_index.has_value() || !NormalizeNativeCommand(command, *callback_index, canonical, _network_own_client_id)) throw std::runtime_error("blocked");
		const auto proof = CaptureNativeIntentProof(command, canonical, _network_own_client_id);
		Json *bundle = &_renderer.building_bundle;
		if (!bundle->at("commands").empty()) throw std::runtime_error("blocked");
		(*bundle)["commands"].push_back({{"command", to_underlying(command)}, {"data", FormatArrayAsHex(canonical)}, {"callback", *callback_index}, {"error", error.base()}, {"company_id", company.base()}});
		(*bundle)["state"] = proof.state;
		(*bundle)["tile_proof"] = proof.tile_proof;
		if (bundle->dump().size() > 48000) throw std::runtime_error("blocked");

		return true;
	} catch (const std::exception &) {
		_renderer.capture_failed = true;
		return false;
	}
}

void RendererBeginOwnedCommand(Commands command, const CommandDataBuffer &data, bool my_command, uint8_t callback)
{
	if (!RendererIsActive() || !my_command || _renderer.active_request.empty()) return;
	const auto bundle = _renderer.bundles.find(_renderer.active_request);
	if (bundle == _renderer.bundles.end() || bundle->second.at("commands").size() != 1) return;
	const Json &expected = bundle->second.at("commands").front();
	if (expected.at("command") != to_underlying(command) || expected.at("data") != FormatArrayAsHex(data) || expected.at("callback") != callback) return;
	_renderer.replay_context = true;
	_renderer.capture_failed = false;
	RendererBeginInputCallback(command);
}

void RendererEndOwnedCommand()
{
	if (!_renderer.replay_context) return;
	RendererEndInputCallback();
	_renderer.replay_context = false;
	if (_renderer.capture_failed) _renderer.callback_failures.insert(_renderer.active_request);
	_renderer.phases[_renderer.active_request] = "completed";
	_renderer.active_request.clear();
	if (_renderer.capture_failed) RenderRejected();
}

Json RendererUICapture(const Json &params)
{
	try {
		const std::string request = params.at("request_id").get<std::string>();
		if (request.empty() || request.size() > 128) return Failure("invalid_request");
		Json input = params;
		const std::string gesture = input.value("gesture", std::string{});
		if ((gesture == "click" || gesture == "drag") && !input.contains("button")) input["button"] = "left";
		const std::string payload = input.dump();
		if (auto cached = _renderer.bundles.find(request); cached != _renderer.bundles.end()) {
			return _renderer.payloads.at(request) == payload ? cached->second : Failure("request_conflict");
		}
		if (!Ready() || !RendererShouldPark() || !_renderer.screenshot || !SameLayout(*_renderer.screenshot)) return Failure("stale_view");
		if (_renderer.input_revision != _lifecycle_revision.load()) {
			EndParking();
			return Failure("stale_view");
		}
		if (!_renderer.active_request.empty() || _renderer.bundles.size() >= MAX_CAPTURED_BUNDLES) return Failure("renderer_busy");
		const std::string image = params.at("image_id").get<std::string>();
		if (image != _renderer.screenshot->id) return Failure("stale_view");
		const std::string authority = params.value("authority_instance_id", _renderer.authority_instance);
		const std::string epoch = params.value("world_epoch", _renderer.world_epoch);
		if (authority.empty() || epoch.empty() || authority.size() > 256 || epoch.size() > 256) return Failure("invalid_request");
		if (_renderer.authority_instance.empty()) { _renderer.authority_instance = authority; _renderer.world_epoch = epoch; }
		if (_renderer.authority_instance != authority || _renderer.world_epoch != epoch) return Failure("world_fence");
		_renderer.building_bundle = BundleBase(request, image, _renderer.parked_frame);
		_renderer.capture_failed = false;
		/* Text replacement and named tool selection cannot post gameplay commands. */
		_renderer.text_only = gesture == "text" || gesture == "control";
		_renderer.capturing = true;
		AutoRestoreBackup company_scope(_current_company, _renderer.company);
		const bool applied = RendererApplyInput(input);
		_renderer.capturing = false;
		_renderer.text_only = false;
		if (!applied || _renderer.capture_failed) {
			Json rejected = Failure("gesture_rejected");
			rejected["request_id"] = request;
			RememberBundle(rejected, payload);
			_renderer.phases[request] = "rejected";
			RenderRejected();
			EndParking();
			return rejected;
		}
		RememberBundle(_renderer.building_bundle, payload);
		_renderer.active_request = request;
		_renderer.screenshot.reset();
		RendererClearAccessSnapshot();
		return _renderer.building_bundle;
	} catch (const std::exception &) {
		_renderer.capturing = false;
		_renderer.text_only = false;
		EndParking();
		return Failure("gesture_rejected");
	}
}

Json RendererUIStatus(const Json &params)
{
	try {
		const std::string request = params.at("request_id").get<std::string>();
		auto cached = _renderer.bundles.find(request);
		if (cached == _renderer.bundles.end()) return Failure("unknown_request");
		Json children = Json::array();

		return {{"bundle", cached->second}, {"pending", _renderer.active_request == request}, {"phase", _renderer.phases.at(request)}, {"callback_rejected", _renderer.callback_failures.contains(request)}, {"children", std::move(children)}};
	} catch (const std::exception &) { return Failure("invalid_request"); }
}

Json RendererUIRelease(const Json &params)
{
	try {
		const std::string request = params.at("request_id").get<std::string>();
		if (request.empty() || request.size() > 128) return Failure("invalid_request");
		const std::string phase = params.value("phase", std::string("commit"));
		auto cached = _renderer.bundles.find(request);
		/* Acknowledgement only deletes this worker's cache, never authoritative work. */
		if (cached == _renderer.bundles.end()) return phase == "ack" ? Json{{"status", "ok"}} : Failure("unknown_request");
		const std::string previous = _renderer.phases.at(request);
		if (phase == "ack") {
			if (_renderer.active_request == request || (previous != "completed" && previous != "rejected")) return Failure("renderer_busy");
			_renderer.bundles.erase(cached);
			_renderer.payloads.erase(request);
			_renderer.callback_failures.erase(request);
			_renderer.phases.erase(request);
			std::erase(_renderer.bundle_order, request);
		} else if (phase == "commit" || phase == "reject") {
			if (previous == "completed") return phase == "commit" ? Json{{"status", "ok"}} : Failure("phase_conflict");
			if (previous == "rejected") return phase == "reject" ? Json{{"status", "ok"}} : Failure("phase_conflict");
			/* After resume, rejection requires the trusted backend's terminal no-dispatch outcome. */
			if (previous == "committed" && phase == "reject" && !params.value("authority_rejected", false)) return Failure("phase_conflict");
			if (!_renderer.active_request.empty() && _renderer.active_request != request) return Failure("renderer_busy");
			if (phase == "reject" || cached->second.at("commands").empty()) {
				_renderer.phases[request] = phase == "reject" ? "rejected" : "completed";
				_renderer.active_request.clear();
				if (phase == "reject") RenderRejected();
			} else {
				_renderer.active_request = request;
				_renderer.phases[request] = "committed";
			}
			EndParking();
		} else return Failure("invalid_request");
		return {{"status", "ok"}};
	} catch (const std::exception &) { return Failure("invalid_request"); }
}

Json RendererControl(const Json &params)
{
	try {
		if (!params.is_object() || !Ready()) return Failure("renderer_not_ready");
		AutoRestoreBackup company_scope(_current_company, _renderer.company);
		if (!_renderer.active_request.empty()) return Failure("renderer_busy");
		const auto now = std::chrono::steady_clock::now();
		if (now - _renderer.control_interval >= std::chrono::seconds(1)) { _renderer.control_interval = now; _renderer.controls_in_interval = 0; }
		if (++_renderer.controls_in_interval > 16) return Failure("rate_limited");
		const std::string action = params.at("action").get<std::string>();
		Window *window = GetMainWindow();
		if (action == "inspect") return Inspect(params);
		if (action == "pan") {
			const int dx = params.at("dx").get<int>();
			const int dy = params.at("dy").get<int>();
			if (abs(static_cast<int64_t>(dx)) > _screen.width || abs(static_cast<int64_t>(dy)) > _screen.height) return Failure("invalid_control");
			auto &viewport = *window->viewport;
			viewport.CancelFollow(*window);
			constexpr int64_t LIMIT = 1LL << 28;
			viewport.scrollpos_x = static_cast<int32_t>(std::clamp<int64_t>(static_cast<int64_t>(viewport.scrollpos_x) + ScaleByZoom(dx, viewport.zoom), -LIMIT, LIMIT));
			viewport.scrollpos_y = static_cast<int32_t>(std::clamp<int64_t>(static_cast<int64_t>(viewport.scrollpos_y) + ScaleByZoom(dy, viewport.zoom), -LIMIT, LIMIT));
			viewport.dest_scrollpos_x = viewport.virtual_left = viewport.scrollpos_x;
			viewport.dest_scrollpos_y = viewport.virtual_top = viewport.scrollpos_y;
			window->SetDirty();
		} else if (action == "zoom") {
			int steps = params.at("steps").get<int>();
			if (steps < -3 || steps > 3) return Failure("invalid_control");
			while (steps < 0) { DoZoomInOutWindow(ZOOM_IN, window); ++steps; }
			while (steps > 0) { DoZoomInOutWindow(ZOOM_OUT, window); --steps; }
			window->SetDirty();
		} else if (action == "close_windows") {
			CloseAllNonVitalWindows();
		} else if (action == "open_window") {
			if (_renderer.company == COMPANY_SPECTATOR) return Failure("invalid_control");
			const std::string kind = params.at("window").get<std::string>();
			if (kind == "company") ShowCompany(_renderer.company);
			else if (kind == "finances") ShowCompanyFinances(_renderer.company);
			else if (kind == "stations") ShowCompanyStations(_renderer.company);
			else if (kind == "road_construction") {
				auto available = [](RoadType type) { return ValParamRoadType(type) && RoadTypeIsRoad(type); };
				if (!available(_last_built_roadtype)) SetDefaultRoadGui();
				if (!available(_last_built_roadtype) || ShowBuildRoadToolbar(_last_built_roadtype) == nullptr) return Failure("invalid_control");
			} else if (kind == "rail_construction") {
				if (!ValParamRailType(_last_built_railtype)) SetDefaultRailGui();
				if (!ValParamRailType(_last_built_railtype) || ShowBuildRailToolbar(_last_built_railtype) == nullptr) return Failure("invalid_control");
			} else if (kind == "water_construction") {
				if (ShowBuildDocksToolbar() == nullptr) return Failure("invalid_control");
			} else if (kind == "airport_construction") {
				if (ShowBuildAirToolbar() == nullptr) return Failure("invalid_control");
			} else if (kind == "landscaping") {
				if (ShowTerraformToolbar() == nullptr) return Failure("invalid_control");
			} else if (kind == "trees") {
				ShowBuildTreesToolbar();
			} else if (kind == "vehicles") {
				const std::string type = params.at("vehicle_type").get<std::string>();
				VehicleType vehicle_type;
				if (type == "rail") vehicle_type = VehicleType::Train;
				else if (type == "road") vehicle_type = VehicleType::Road;
				else if (type == "ship") vehicle_type = VehicleType::Ship;
				else if (type == "air") vehicle_type = VehicleType::Aircraft;
				else return Failure("invalid_control");
				ShowVehicleListWindow(_renderer.company, vehicle_type);
			} else return Failure("invalid_control");
		} else return Failure("invalid_control");
		EndParking();
		return {{"status", "ok"}};
	} catch (const std::exception &) {
		return Failure("invalid_control");
	}
}

Json RendererCapture(const Json &params)
{
	try {
		if (!params.is_object() || !params.empty() || !Ready()) return Failure("renderer_not_ready");
		AutoRestoreBackup company_scope(_current_company, _renderer.company);
		if (!_renderer.active_request.empty()) return Failure("renderer_busy");
#ifndef WITH_PNG
		return Failure("png_required");
#else
		const auto now = std::chrono::steady_clock::now();
		if (RendererShouldPark() && _renderer.screenshot && _renderer.image_metadata.is_object()) return _renderer.image_metadata;
		if (now - _renderer.last_capture < std::chrono::seconds(1)) return Failure("rate_limited");
		if (_screen.width < 1 || _screen.width > MAX_CAPTURE_WIDTH || _screen.height < 1 || _screen.height > MAX_CAPTURE_HEIGHT || !PrepareDirectory()) return Failure("capture_unavailable");
		/* Discard old-world input state before recording provenance for this PNG. */
		const uint64_t input_revision = _lifecycle_revision.load();
		if (_renderer.input_revision != input_revision) {
			RendererResetInputProvenance();
			_renderer.input_revision = input_revision;
		}
		const uint32_t duration = ParkDurationMs();
		_renderer.parked = duration != 0;
		_renderer.parked_frame = _frame_counter;
		_renderer.park_revision = _lifecycle_revision.load();
		_renderer.park_deadline = now + std::chrono::milliseconds(duration);
		/* Refresh only the frame genuinely replayed before parking; never acknowledge queued state. */
		if (_renderer.parked) MyClient::SendAck();
		_renderer.last_capture = now;
		/* Ordinary window/viewport drawing; never render alternate map/heightmap exports. */
		UpdateWindows();
		UndrawMouseCursor();
		DrawDirtyBlocks();
		const std::string image_id = OpaqueId();
		auto snapshot = Snapshot(image_id);
		RendererRememberAccessSnapshot(image_id);
		const uint64_t tick = _networking ? _frame_counter : TimerGameTick::counter;
		std::vector<uint8_t> rgb(static_cast<size_t>(_screen.width) * _screen.height * 3);
		const auto *pixels = static_cast<const Colour *>(_screen.dst_ptr);
		for (int y = 0; y < _screen.height; ++y) {
			for (int x = 0; x < _screen.width; ++x) {
				const Colour &colour = pixels[static_cast<size_t>(y) * _screen.pitch + x];
				const size_t offset = (static_cast<size_t>(y) * _screen.width + x) * 3;
				rgb[offset] = colour.r; rgb[offset + 1] = colour.g; rgb[offset + 2] = colour.b;
			}
		}
		/* This is an isolated worker: encoding cannot hold the authoritative world's lock. */
		RendererImage image = EncodeRendererImage(rgb, _screen.width, _screen.height);
		const std::filesystem::path path = std::filesystem::path(_renderer.directory) / ("frame-" + image_id + ".png");
		auto file = FileHandle::Open(path.string(), "wbx");
		if (!file.has_value()) return Failure("capture_unavailable");
		const bool written = fwrite(image.bytes.data(), 1, image.bytes.size(), *file) == image.bytes.size() && fflush(*file) == 0;
		file->Close();
		std::error_code error;
		if (!written) { std::filesystem::remove(path, error); return Failure("capture_unavailable"); }
		std::filesystem::permissions(path, std::filesystem::perms::owner_read | std::filesystem::perms::owner_write, std::filesystem::perm_options::replace, error);
		if (error) { std::filesystem::remove(path, error); return Failure("capture_unavailable"); }
		_renderer.artifacts.push_back(path);
		while (_renderer.artifacts.size() > MAX_RETAINED_IMAGES) {
			std::filesystem::remove(_renderer.artifacts.front(), error);
			_renderer.artifacts.pop_front();
		}
		_renderer.screenshot = std::move(snapshot);
		_renderer.image_metadata = {{"image_id", image_id}, {"path", path.string()}, {"width", _screen.width}, {"height", _screen.height}, {"tick", tick}, {"source_frame", _frame_counter}, {"screenshot_id", image_id}, {"content_type", image.content_type}, {"park_ttl_ms", duration}};
		return _renderer.image_metadata;
#endif
	} catch (const std::exception &) {
		EndParking();
		return Failure("capture_unavailable");
	}
}

} // namespace Integration
