/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 2.
 */

/** @file actions_story.cpp Normal GameScript story buttons and retained question presentations. */

#include "../stdafx.h"
#include "actions_story.h"
#include "actions_internal.h"
#include "renderer_input.h"
#include "bridge_hooks.h"
#include "../3rdparty/monocypher/monocypher.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../goal_base.h"
#include "../goal_cmd.h"
#include "../engine_base.h"
#include "../industry.h"
#include "../misc/endian_buffer.hpp"
#include "../network/network_base.h"
#include "../story_base.h"
#include "../story_cmd.h"
#include "../station_base.h"
#include "../string_func.h"
#include "../strings_func.h"
#include "../timer/timer_game_tick.h"
#include "../town.h"
#include "../vehicle_base.h"
#include "../vehicle_func.h"

#include <map>
#include <memory>
#include <deque>

#include "table/strings.h"
#include "../safeguards.h"

namespace Integration {
namespace {

using Detail::InvalidParameters;
using Detail::Parameters;

static constexpr std::array<std::string_view, to_underlying(GoalQuestionButton::End)> BUTTON_NAMES = {
	"cancel", "ok", "no", "yes", "decline", "accept", "ignore", "retry", "previous", "next", "stop", "start", "go", "continue", "restart", "postpone", "surrender", "close",
};
static constexpr size_t PREVIEW_BYTES = 512;

/** A replacement company never inherits an earlier company's text inbox. */
struct Recipient {
	uint32_t id;
	uint64_t generation;
	auto operator<=>(const Recipient &) const = default;
};

struct Question {
	uint16_t id;
	uint64_t generation;
	uint32_t target;
	GoalQuestionButtons buttons;
	GoalQuestionType type;
	EncodedString text;
};

using QuestionInbox = std::deque<std::shared_ptr<const Question>>;
/* Text is an independent company presentation inbox, not a mirror of native
 * client popup z-order. Native windows permit any number of duplicate IDs; keep
 * each accepted issuance without a TTL, LRU or artificial duplicate limit.
 * Client-private presentations stay in their genuine native windows only. */
static std::map<uint16_t, std::map<Recipient, QuestionInbox>> _questions;
static uint64_t _last_question_generation = 0;

static constexpr size_t CUSTOM_NEWS_ROWS = 1024;
static constexpr size_t CUSTOM_NEWS_BYTES = 4 * 1024 * 1024;
static constexpr size_t CUSTOM_NEWS_ITEM_BYTES = 65536;
static constexpr size_t CUSTOM_NEWS_REPORT_BYTES = 40 * 1024;

/** Historical values only: never look up an old numeric reference while reading. */
struct CustomNewsReference {
	enum class Kind { None, Tile, Vehicle, Station, Industry, Town, Engine } kind = Kind::None;
	uint32_t id = 0;
	uint64_t generation = 0;
	TileIndex location = INVALID_TILE;
};

struct CustomNews {
	uint64_t sequence;
	uint64_t tick;
	TimerGameCalendar::Date date;
	NewsType type;
	CustomNewsReference reference;
	EncodedString text;
};

struct CustomNewsInbox {
	uint64_t generation = 0;
	uint64_t last_sequence = 0;
	uint64_t history_floor = 0;
	bool exhausted = false;
	size_t encoded_bytes = 0;
	std::deque<CustomNews> rows;
};

static std::array<CustomNewsInbox, MAX_COMPANIES> _custom_news;

CustomNewsInbox &NewsInbox(const Company *company)
{
	auto &inbox = _custom_news[company->index.base()];
	if (inbox.generation != company->GetInstanceGeneration()) {
		inbox = {};
		inbox.generation = company->GetInstanceGeneration();
	}
	return inbox;
}

CustomNewsInbox &VisibleNews()
{
	const Company *company = Company::GetIfValid(_current_company);
	if (company == nullptr) throw InvalidParameters("News recipient is unavailable");
	auto &inbox = NewsInbox(company);
	if (inbox.exhausted) throw InvalidParameters("News history is unavailable");
	return inbox;
}

Json NewsCursor(const CustomNewsInbox &inbox, uint64_t sequence)
{
	return {{"id", sequence}, {"generation", inbox.generation}, {"engine_instance_id", InstanceId()}};
}

uint64_t ReadNewsCursor(const Json &cursor, const CustomNewsInbox &inbox, bool item)
{
	if (!cursor.is_object() || cursor.size() != 3 || !cursor.contains("id") || !cursor.contains("generation") || !cursor.contains("engine_instance_id")) {
		throw InvalidParameters("News requires a complete recipient cursor");
	}
	if (!cursor.at("engine_instance_id").is_string() || cursor.at("engine_instance_id").get_ref<const std::string &>() != InstanceId()) {
		throw InvalidParameters("News cursor belongs to another engine instance");
	}
	const auto &generation = cursor.at("generation");
	if (!generation.is_number_integer() || (!generation.is_number_unsigned() && generation.get<int64_t>() < 1) || generation.get<uint64_t>() != inbox.generation) {
		throw InvalidParameters("News cursor belongs to another recipient");
	}
	uint64_t sequence = static_cast<uint64_t>(Parameters(cursor).Int("id", item ? 1 : 0, INT64_MAX));
	if (sequence > inbox.last_sequence) throw InvalidParameters("News cursor is beyond the latest issuance");
	return sequence;
}

CustomNewsReference SnapshotNewsReference(const NewsReference &reference)
{
	using Kind = CustomNewsReference::Kind;
	return std::visit([](const auto &id) -> CustomNewsReference {
		using T = std::remove_cvref_t<decltype(id)>;
		if constexpr (std::is_same_v<T, std::monostate>) {
			return {};
		} else if constexpr (std::is_same_v<T, TileIndex>) {
			return {Kind::Tile, 0, 0, id};
		} else if constexpr (std::is_same_v<T, EngineID>) {
			return {Kind::Engine, id.base()};
		} else {
			CustomNewsReference result;
			result.id = id.base();
			if constexpr (std::is_same_v<T, VehicleID>) {
				const Vehicle *vehicle = Vehicle::Get(id);
				result.kind = Kind::Vehicle;
				result.generation = vehicle->GetInstanceGeneration();
				result.location = TileXY(Clamp(vehicle->x_pos / static_cast<int>(TILE_SIZE), 0, static_cast<int>(Map::MaxX())), Clamp(vehicle->y_pos / static_cast<int>(TILE_SIZE), 0, static_cast<int>(Map::MaxY())));
			} else if constexpr (std::is_same_v<T, StationID>) {
				const Station *station = Station::Get(id);
				result.kind = Kind::Station;
				result.generation = station->GetInstanceGeneration();
				result.location = station->xy;
			} else if constexpr (std::is_same_v<T, IndustryID>) {
				const Industry *industry = Industry::Get(id);
				result.kind = Kind::Industry;
				result.generation = industry->GetInstanceGeneration();
				result.location = industry->location.tile + TileDiffXY(1, 1);
			} else if constexpr (std::is_same_v<T, TownID>) {
				const Town *town = Town::Get(id);
				result.kind = Kind::Town;
				result.generation = town->GetInstanceGeneration();
				result.location = town->xy;
			}
			return result;
		}
	}, reference);
}

Json NewsReferenceMetadata(const CustomNewsReference &reference)
{
	using Kind = CustomNewsReference::Kind;
	if (reference.kind == Kind::None) return nullptr;
	if (reference.kind == Kind::Tile) return Json{{"kind", "tile"}, {"location", Detail::TileCoordinates(reference.location)}};
	if (reference.kind == Kind::Engine) return Json{{"kind", "engine"}, {"engine_id", reference.id}};
	const char *kind = reference.kind == Kind::Vehicle ? "vehicle" : reference.kind == Kind::Station ? "station" : reference.kind == Kind::Industry ? "industry" : "town";
	return {{"kind", kind}, {"target", {{"id", reference.id}, {"generation", reference.generation}, {"engine_instance_id", InstanceId()}}},
		{"location", IsValidTile(reference.location) ? Detail::TileCoordinates(reference.location) : Json(nullptr)}};
}

Json CustomNewsMetadata(const CustomNewsInbox &inbox, const CustomNews &news)
{
	static constexpr std::array<std::string_view, to_underlying(NewsType::End)> TYPES = {
		"arrival_company", "arrival_other", "accident", "accident_other", "company_info", "industry_open", "industry_close", "economy",
		"industry_company", "industry_other", "industry_nobody", "advice", "new_vehicles", "acceptance", "subsidies", "general",
	};
	auto date = TimerGameCalendar::ConvertDateToYMD(news.date);
	return {{"news_id", NewsCursor(inbox, news.sequence)}, {"issued_tick", news.tick},
		{"calendar_date", {{"year", date.year.base()}, {"month", date.month + 1}, {"day", date.day}}},
		{"news_type", TYPES[to_underlying(news.type)]}, {"reference", NewsReferenceMetadata(news.reference)}};
}

Json QueryCustomNews(const Json &parameters)
{
	if (!parameters.is_object()) throw InvalidParameters("Invalid news parameters");
	for (auto it = parameters.begin(); it != parameters.end(); ++it) {
		if (it.key() != "after" && it.key() != "limit") throw InvalidParameters("Unknown news parameter");
	}
	auto &inbox = VisibleNews();
	uint64_t after = parameters.contains("after") ? ReadNewsCursor(parameters.at("after"), inbox, false) : 0;
	size_t limit = static_cast<size_t>(Parameters(parameters).Int("limit", 1, 32, 32));
	uint64_t next = std::max(after, inbox.history_floor);
	Json items = Json::array();
	for (const auto &news : inbox.rows) {
		if (news.sequence <= next) continue;
		if (items.size() == limit) break;
		items.push_back(CustomNewsMetadata(inbox, news));
		next = news.sequence;
	}
	Json result = Detail::SnapshotBase();
	result.update({{"history_scope", "current_engine_instance"}, {"history_start", NewsCursor(inbox, inbox.history_floor)},
		{"latest", NewsCursor(inbox, inbox.last_sequence)}, {"gap", after < inbox.history_floor}, {"items", std::move(items)},
		{"next", NewsCursor(inbox, next)}, {"complete", next == inbox.last_sequence}});
	if (result.dump().size() > CUSTOM_NEWS_REPORT_BYTES) throw InvalidParameters("News report exceeds byte budget");
	return result;
}

std::optional<Recipient> CompanyRecipient(CompanyID id)
{
	const Company *company = Company::GetIfValid(id);
	if (company == nullptr) return std::nullopt;
	return Recipient{id.base(), company->GetInstanceGeneration()};
}

bool RecipientAlive(const Recipient &recipient)
{
	auto current = CompanyRecipient(CompanyID(static_cast<uint8_t>(recipient.id)));
	return current.has_value() && *current == recipient;
}

void PruneRecipients(std::map<Recipient, QuestionInbox> &slots)
{
	std::erase_if(slots, [](const auto &item) { return !RecipientAlive(item.first); });
}

const QuestionInbox &VisibleQuestions(uint16_t id)
{
	auto recipient = CompanyRecipient(_current_company);
	if (!recipient.has_value()) throw InvalidParameters("Question recipient is unavailable");
	auto found = _questions.find(id);
	if (found == _questions.end()) throw InvalidParameters("Question is unavailable");
	auto slot = found->second.find(*recipient);
	if (slot == found->second.end() || slot->second.empty()) throw InvalidParameters("Question is unavailable");
	return slot->second;
}

Json QuestionHandle(const Question &question)
{
	return {{"id", question.id}, {"generation", question.generation}, {"engine_instance_id", InstanceId()}};
}

/** Verify a complete public handle before narrowing a native ID. */
uint64_t HandleGeneration(const Json &handle, uint64_t maximum_id, uint64_t &id)
{
	if (!handle.is_object() || handle.size() != 3 || !handle.contains("id") || !handle.contains("generation") || !handle.contains("engine_instance_id")) throw InvalidParameters("Target requires a complete handle");
	if (!handle.at("engine_instance_id").is_string() || handle.at("engine_instance_id").get_ref<const std::string &>() != InstanceId()) throw InvalidParameters("Target belongs to another engine instance");
	Parameters p(handle);
	id = static_cast<uint64_t>(p.Int("id", 0, maximum_id));
	const auto &generation = handle.at("generation");
	if (!generation.is_number_integer() || (!generation.is_number_unsigned() && generation.get<int64_t>() < 0)) throw InvalidParameters("Invalid target generation");
	return generation.get<uint64_t>();
}

const Question &ReadQuestion(const Json &parameters)
{
	uint64_t id;
	auto generation = HandleGeneration(parameters.at("question_id"), UINT16_MAX, id);
	const auto &inbox = VisibleQuestions(static_cast<uint16_t>(id));
	auto found = std::ranges::lower_bound(inbox, generation, {}, [](const auto &question) { return question->generation; });
	if (found == inbox.end() || (*found)->generation != generation) throw InvalidParameters("Question target is stale");
	return **found;
}

GoalQuestionButton ReadButton(const Parameters &p)
{
	auto name = p.Text("button");
	auto found = std::ranges::find(BUTTON_NAMES, name);
	if (found == BUTTON_NAMES.end()) throw InvalidParameters("Unknown question button");
	return static_cast<GoalQuestionButton>(found - BUTTON_NAMES.begin());
}

void RequireButton(const Question &question, GoalQuestionButton button)
{
	if (button >= GoalQuestionButton::End || !question.buttons.Test(button)) throw InvalidParameters("Question did not offer this button");
}

bool PageReadable(const StoryPage *page)
{
	return page->company == CompanyID::Invalid() || Company::IsValidID(page->company);
}

bool PageInteractive(const StoryPage *page)
{
	return page->company == CompanyID::Invalid() || page->company == _current_company;
}

template <typename T>
const T *ReadHandle(const Json &parameters, const char *key)
{
	uint64_t id;
	auto generation = HandleGeneration(parameters.at(key), decltype(T::index)::End().base() - 1, id);
	const T *item = T::GetIfValid(id);
	if (item == nullptr || item->GetInstanceGeneration() != generation) throw InvalidParameters("Target is invalid or stale");
	return item;
}

const StoryPage *PageFor(const StoryPageElement *element)
{
	const StoryPage *page = StoryPage::GetIfValid(element->page);
	if (page == nullptr || !PageReadable(page)) throw InvalidParameters("Story page is unavailable");
	return page;
}

const StoryPage *ReadPage(const Json &parameters)
{
	const StoryPage *page = ReadHandle<StoryPage>(parameters, "page_id");
	if (!PageReadable(page)) throw InvalidParameters("Story page is unavailable");
	return page;
}

/** Native-pool traversal is bounded by 64000 slots, independent of output paging. */
std::string PageVersion(const StoryPage *page, bool authoritative)
{
	crypto_blake2b_ctx hash;
	crypto_blake2b_init(&hash, 32);
	auto value = [&](const auto &item) {
		auto bytes = EndianBufferWriter<CommandDataBuffer>::FromValue(item);
		crypto_blake2b_update(&hash, bytes.data(), bytes.size());
	};
	value(std::string_view("OpenTTD integration story v1"));
	value(authoritative);
	if (authoritative) {
		value(std::string(InstanceId()));
		value(page->GetInstanceGeneration());
	}
	value(page->index.base());
	value(page->company.base());
	value(page->sort_value);
	value(page->date.base());
	value(page->title);
	/* Pool index order is deterministic; sort_value also binds displayed order. */
	for (const StoryPageElement *element : StoryPageElement::Iterate()) {
		if (element->page != page->index) continue;
		value(element->index.base());
		if (authoritative) value(element->GetInstanceGeneration());
		value(element->sort_value);
		value(element->type);
		value(element->referenced_id);
		value(element->text);
		if (element->type == StoryPageElementType::Goal) {
			const Goal *goal = element->referenced_id < 64000 ? Goal::GetIfValid(element->referenced_id) : nullptr;
			value(goal != nullptr);
			if (goal != nullptr) {
				if (authoritative) value(goal->GetInstanceGeneration());
				value(goal->text);
				value(goal->completed);
			}
		}
	}
	std::array<uint8_t, 32> result;
	crypto_blake2b_final(&hash, result.data());
	return std::string(authoritative ? "spv1:" : "spi1:") + FormatArrayAsHex(result);
}

void RequirePageVersion(const Json &parameters, const StoryPage *page)
{
	if (Parameters(parameters).Text("page_version", "", 69) != PageVersion(page, true)) throw InvalidParameters("Story page changed; read its current context again");
}

const StoryPageElement *InteractiveElement(const Json &parameters, StoryPageElementType type)
{
	const StoryPageElement *element = ReadHandle<StoryPageElement>(parameters, "element_id");
	const StoryPage *page = PageFor(element);
	if (!PageInteractive(page) || element->type != type) throw InvalidParameters("Story button is unavailable to this company or has the wrong selection type");
	RequirePageVersion(parameters, page);
	return element;
}

void RequireSelectableVehicle(const StoryPageElement *element, const Vehicle *vehicle)
{
	if (vehicle == nullptr || !IsCompanyBuildableVehicleType(vehicle) || !vehicle->IsPrimaryVehicle()) throw InvalidParameters("Story selection requires a live primary vehicle");
	auto type = StoryPageButtonData{element->referenced_id}.GetVehicleType();
	if (type != VehicleType::Invalid && type != vehicle->type) throw InvalidParameters("Vehicle does not match the story button's selection type");
}

const Vehicle *ReadSelectableVehicle(const Json &parameters, const StoryPageElement *element)
{
	const Vehicle *vehicle = ReadHandle<Vehicle>(parameters, "vehicle_id");
	RequireSelectableVehicle(element, vehicle);
	return vehicle;
}

size_t Utf8End(std::string_view text, size_t end)
{
	end = std::min(end, text.size());
	while (end > 0 && end < text.size() && (static_cast<uint8_t>(text[end]) & 0xC0) == 0x80) --end;
	return end;
}

std::string Preview(std::string_view text)
{
	return std::string(text.substr(0, Utf8End(text, PREVIEW_BYTES)));
}

Json TextChunk(std::string text, const Parameters &p)
{
	size_t offset = static_cast<size_t>(p.Int("text_offset", 0, INT32_MAX, 0));
	size_t limit = static_cast<size_t>(p.Int("text_limit", 1, 8192, 4096));
	if (offset > text.size() || Utf8End(text, offset) != offset) throw InvalidParameters("Text offset is outside the text or splits UTF-8");
	size_t end = Utf8End(text, offset + limit);
	if (end == offset && offset < text.size()) throw InvalidParameters("Text limit must fit the next UTF-8 code point");
	return {{"text", text.substr(offset, end - offset)}, {"text_offset", offset}, {"text_bytes", text.size()}, {"next_text_offset", end < text.size() ? Json(end) : Json(nullptr)}};
}

const char *ElementType(StoryPageElementType type)
{
	switch (type) {
		case StoryPageElementType::Text: return "text";
		case StoryPageElementType::Location: return "location";
		case StoryPageElementType::Goal: return "goal";
		case StoryPageElementType::ButtonPush: return "button";
		case StoryPageElementType::ButtonTile: return "tile_button";
		case StoryPageElementType::ButtonVehicle: return "vehicle_button";
		default: return "unknown";
	}
}

Json PageMetadata(const StoryPage *page)
{
	std::string title = page->title.GetDecodedString();
	return {{"page_id", Detail::Handle(page)}, {"book_company_id", page->company == CompanyID::Invalid() ? Json(nullptr) : Json(page->company.base())},
		{"sort_order", page->sort_value}, {"date", page->date.base()}, {"title", Preview(title)}, {"title_truncated", title.size() > PREVIEW_BYTES}, {"interactive", PageInteractive(page)}};
}

Json ElementMetadata(const StoryPageElement *element)
{
	std::string text = element->text.GetDecodedString();
	Json result = {{"element_id", Detail::Handle(element)}, {"sort_order", element->sort_value}, {"type", ElementType(element->type)}, {"text", Preview(text)}, {"text_truncated", text.size() > PREVIEW_BYTES}};
	if (element->type == StoryPageElementType::Location && IsValidTile(TileIndex(element->referenced_id))) result["location"] = Detail::TileCoordinates(TileIndex(element->referenced_id));
	if (element->type == StoryPageElementType::Goal) {
		const Goal *goal = element->referenced_id < 64000 ? Goal::GetIfValid(element->referenced_id) : nullptr;
		if (goal != nullptr) {
			std::string goal_text = goal->text.GetDecodedString();
			result["goal_id"] = Detail::Handle(goal);
			result["text"] = Preview(goal_text);
			result["text_truncated"] = goal_text.size() > PREVIEW_BYTES;
			result["completed"] = goal->completed;
		}
	}
	if (element->type == StoryPageElementType::ButtonVehicle) {
		auto type = StoryPageButtonData{element->referenced_id}.GetVehicleType();
		result["vehicle_type"] = type == VehicleType::Invalid ? Json(nullptr) : Json(Detail::VehicleTypeName(type));
	}
	return result;
}

Json QuestionMetadata(const Question &question)
{
	std::string text = question.text.GetDecodedString();
	Json buttons = Json::array();
	for (GoalQuestionButton button : question.buttons) buttons.push_back(BUTTON_NAMES[to_underlying(button)]);
	static constexpr std::array<std::string_view, 4> TYPES = {"question", "information", "warning", "error"};
	return {{"question_id", QuestionHandle(question)}, {"type", TYPES[to_underlying(question.type)]}, {"buttons", std::move(buttons)}, {"text", Preview(text)}, {"text_truncated", text.size() > PREVIEW_BYTES}};
}

template <typename T, typename Filter, typename Serialize>
Json StoryPoolPage(const Parameters &p, Filter filter, Serialize serialize)
{
	uint64_t next = static_cast<uint64_t>(p.Int("after_id", -1, UINT32_MAX, -1) + 1);
	size_t limit = static_cast<size_t>(p.Int("limit", 1, 32, 32));
	uint scanned = 0;
	Json items = Json::array();
	size_t item_bytes = 0;
	while (next < T::GetPoolSize() && scanned < 4096 && items.size() < limit) {
		const T *item = T::GetIfValid(next);
		if (item != nullptr && filter(item)) {
			Json encoded = serialize(item);
			size_t bytes = encoded.dump().size();
			if (!items.empty() && item_bytes + bytes > 32000) break;
			item_bytes += bytes;
			items.push_back(std::move(encoded));
		}
		++next;
		++scanned;
	}
	return {{"items", std::move(items)}, {"next_after_id", next < T::GetPoolSize() ? Json(next - 1) : Json(nullptr)}};
}

template <Commands Tcommand>
auto Decode(const CommandDataBuffer &data)
{
	using Args = typename CommandTraits<Tcommand>::Args;
	auto args = EndianBufferReader::ToValue<Args>(data);
	if (EndianBufferWriter<CommandDataBuffer>::FromValue(args) != data) throw InvalidParameters("Malformed native story command");
	return args;
}

std::string QuestionTextHash(const EncodedString &text)
{
	/* Encoded text may contain native string codes; a byte digest avoids a JSON
	 * string carrying that private format and stays independent of local language. */
	auto bytes = EndianBufferWriter<CommandDataBuffer>::FromValue(text);
	std::array<uint8_t, 32> hash;
	crypto_blake2b(hash.data(), hash.size(), bytes.data(), bytes.size());
	return FormatArrayAsHex(hash);
}

Json QuestionProof(const Question &question)
{
	return {{"question_id", question.id}, {"type", to_underlying(question.type)}, {"buttons", question.buttons.base()},
		{"scope", "company"}, {"target", question.target}, {"text_hash", QuestionTextHash(question.text)}};
}

void RequireQuestionOwner(ClientID owner)
{
	const NetworkClientInfo *client = owner == ClientID::Invalid ? nullptr : NetworkClientInfo::GetByClientID(owner);
	if (client == nullptr || client->client_playas != _current_company) throw InvalidParameters("Question witness requires the bound native company connection");
}

const StoryPageElement *NativeElement(const CommandDataBuffer &data, const Vehicle **selected = nullptr)
{
	auto [tile, id, reference] = Decode<Commands::StoryPageButton>(data);
	const StoryPageElement *element = StoryPageElement::GetIfValid(id);
	if (element == nullptr || !PageInteractive(PageFor(element))) throw InvalidParameters("Story button is unavailable to this company");
	switch (element->type) {
		case StoryPageElementType::ButtonPush:
			if (tile != TileIndex{} || reference != VehicleID::Invalid()) throw InvalidParameters("Push story button has unexpected selection parameters");
			break;
		case StoryPageElementType::ButtonTile:
			if (!IsValidTile(tile) || reference != VehicleID::Invalid()) throw InvalidParameters("Invalid story tile selection");
			break;
		case StoryPageElementType::ButtonVehicle: {
			if (tile != TileIndex{}) throw InvalidParameters("Vehicle story button has an unexpected tile");
			const Vehicle *vehicle = Vehicle::GetIfValid(reference);
			RequireSelectableVehicle(element, vehicle);
			if (selected != nullptr) *selected = vehicle;
			break;
		}
		default: throw InvalidParameters("Story element is not an interactive button");
	}
	return element;
}

} // namespace

void StoryQuestionPresented(uint16_t id, uint32_t target, bool is_client, GoalQuestionButtons buttons, GoalQuestionType type, const EncodedString &text)
{
	/* Native enum/recipient/content validation happens before this Execute hook. */
	if (_last_question_generation == UINT64_MAX) throw InvalidParameters("Question issuance namespace exhausted");
	const uint64_t generation = ++_last_question_generation;
	/* A visual worker reads its actual native windows. In particular, a
	 * client-private question must never become a company text observation. */
	if (is_client || RendererIsActive()) return;
	auto question = std::make_shared<Question>(Question{id, generation, target, buttons, type, text});
	auto &slots = _questions[id];
	PruneRecipients(slots);
	for (const Company *company : Company::Iterate()) {
		if (target != CompanyID::Invalid().base() && target != company->index.base()) continue;
		slots[*CompanyRecipient(company->index)].push_back(question);
	}
	if (slots.empty()) _questions.erase(id);
}

void StoryQuestionAnswered(CompanyID company, uint16_t id, std::optional<uint64_t> selected_generation)
{
	auto found = _questions.find(id);
	if (found == _questions.end()) return;
	PruneRecipients(found->second);
	auto recipient = CompanyRecipient(company);
	if (recipient.has_value()) {
		auto slot = found->second.find(*recipient);
		if (slot != found->second.end()) {
			auto &inbox = slot->second;
			if (selected_generation.has_value()) {
				auto selected = std::ranges::lower_bound(inbox, *selected_generation, {}, [](const auto &question) { return question->generation; });
				/* An intervening close may already have consumed this issuance.
				 * Never fall back and consume an unrelated duplicate. */
				if (selected != inbox.end() && (*selected)->generation == *selected_generation) inbox.erase(selected);
			} else if (!inbox.empty()) {
				/* This text frontend has deterministic newest-first close order;
				 * native client windows can independently change their z-order. */
				inbox.pop_back();
			}
			if (inbox.empty()) found->second.erase(slot);
		}
	}
	if (found->second.empty()) _questions.erase(found);
}

void StoryQuestionsClosed(uint16_t id)
{
	auto found = _questions.find(id);
	if (found == _questions.end()) return;
	PruneRecipients(found->second);
	for (auto &[recipient, inbox] : found->second) {
		/* Native deity CloseWindowById closes one matching popup, not all. */
		if (!inbox.empty()) inbox.pop_back();
	}
	std::erase_if(found->second, [](const auto &item) { return item.second.empty(); });
	if (found->second.empty()) _questions.erase(found);
}

void CustomNewsPresented(NewsType type, CompanyID target, const NewsReference &reference, const EncodedString &text)
{
	/* Native validation precedes this Execute-only hook. No formatting, company
	 * scope switch, callback or bridge initialization belongs in this path. */
	for (size_t id = 0; id < _custom_news.size(); ++id) {
		const Company *company = Company::GetIfValid(CompanyID(static_cast<uint8_t>(id)));
		if (company == nullptr || company->GetInstanceGeneration() != _custom_news[id].generation) _custom_news[id] = {};
	}
	const size_t bytes = text.GetEncodedSize();
	const CustomNewsReference snapshot = bytes <= CUSTOM_NEWS_ITEM_BYTES ? SnapshotNewsReference(reference) : CustomNewsReference{};
	for (const Company *company : Company::Iterate()) {
		if (target != INVALID_OWNER && target != company->index) continue;
		auto &inbox = NewsInbox(company);
		if (inbox.exhausted) continue;
		if (inbox.last_sequence == INT64_MAX) {
			inbox.exhausted = true;
			inbox.rows.clear();
			inbox.encoded_bytes = 0;
			inbox.history_floor = inbox.last_sequence;
			continue;
		}
		++inbox.last_sequence;
		if (bytes > CUSTOM_NEWS_ITEM_BYTES) {
			/* Loss is a prefix watermark, never a partial or decoded substitute. */
			inbox.rows.clear();
			inbox.encoded_bytes = 0;
			inbox.history_floor = inbox.last_sequence;
			continue;
		}
		inbox.rows.push_back({inbox.last_sequence, TimerGameTick::counter, TimerGameCalendar::date, type, snapshot, text});
		inbox.encoded_bytes += bytes;
		while (inbox.rows.size() > CUSTOM_NEWS_ROWS || inbox.encoded_bytes > CUSTOM_NEWS_BYTES) {
			inbox.history_floor = inbox.rows.front().sequence;
			inbox.encoded_bytes -= inbox.rows.front().text.GetEncodedSize();
			inbox.rows.pop_front();
		}
	}
}

void ResetStoryPresentations()
{
	_questions.clear();
	for (size_t id = 0; id < _custom_news.size(); ++id) {
		auto &inbox = _custom_news[id];
		const Company *company = Company::GetIfValid(CompanyID(static_cast<uint8_t>(id)));
		if (company == nullptr || company->GetInstanceGeneration() != inbox.generation) {
			inbox = {};
			continue;
		}
		inbox.rows.clear();
		inbox.encoded_bytes = 0;
		inbox.history_floor = inbox.last_sequence;
	}
}
uint64_t LastStoryQuestionGeneration() { return _last_question_generation; }

namespace Detail {

const EncodedString &CustomNewsText(const Json &target)
{
	const auto &inbox = VisibleNews();
	const uint64_t sequence = ReadNewsCursor(target, inbox, true);
	auto found = std::ranges::lower_bound(inbox.rows, sequence, {}, &CustomNews::sequence);
	if (found == inbox.rows.end() || found->sequence != sequence) throw InvalidParameters("News target is unavailable");
	return found->text;
}

Json ResolveStoryControlReference(const std::string &kind, uint32_t id)
{
	if (kind == "question") {
		if (id > UINT16_MAX) throw InvalidParameters("Question is unavailable");
		const auto &inbox = VisibleQuestions(static_cast<uint16_t>(id));
		if (inbox.size() != 1) throw InvalidParameters("Question selection is ambiguous");
		return {{"kind", kind}, {"reference", QuestionHandle(*inbox.front())}};
	}
	if (id >= 64000) throw InvalidParameters("Story target is unavailable");
	const StoryPageElement *element = nullptr;
	const StoryPage *page = nullptr;
	if (kind == "story_page") {
		page = StoryPage::GetIfValid(StoryPageID(id));
	} else if (kind == "story_element") {
		element = StoryPageElement::GetIfValid(StoryPageElementID(id));
		if (element != nullptr) page = StoryPage::GetIfValid(element->page);
	}
	if (page == nullptr || !PageInteractive(page)) throw InvalidParameters("Story target is unavailable");
	AdmitWorldTextStart();
	return {{"kind", kind}, {"reference", element == nullptr ? Handle(page) : Handle(element)},
		{"page", Handle(page)}, {"page_version", PageVersion(page, true)}};
}

Json MakeStoryQuestionWitness(uint16_t id, GoalQuestionType type, GoalQuestionButtons buttons, const EncodedString &text)
{
	auto hash = QuestionTextHash(text);
	for (char &c : hash) if (c >= 'A' && c <= 'F') c = static_cast<char>(c + ('a' - 'A'));
	return {{"source", "native_goal_question_window_v1"}, {"question_id", id}, {"type", to_underlying(type)},
		{"buttons", buttons.base()}, {"text_hash", std::move(hash)}};
}

ActionResult ValidateStoryQuestionWitness(const CommandDataBuffer &data, const Json &witness, ClientID owner)
{
	try {
		RequireCompany();
		RequireQuestionOwner(owner);
		auto [id, button] = Decode<Commands::GoalQuestionAnswer>(data);
		if (!witness.is_object() || witness.size() != 5 || !witness.contains("source") || !witness.contains("question_id") ||
				!witness.contains("type") || !witness.contains("buttons") || !witness.contains("text_hash") ||
				witness.at("source") != "native_goal_question_window_v1") throw InvalidParameters("Invalid native question window witness");
		Parameters p(witness);
		if (p.Int("question_id", 0, UINT16_MAX) != id) throw InvalidParameters("Question window does not match the command");
		const auto type = static_cast<GoalQuestionType>(p.Int("type", 0, to_underlying(GoalQuestionType::End) - 1));
		const auto buttons = GoalQuestionButtons(static_cast<uint32_t>(p.Int("buttons", 0, UINT32_MAX)));
		if (!buttons.IsValid() || buttons.Count() > 3 || (type == GoalQuestionType::Question && buttons.None()) ||
				button >= GoalQuestionButton::End || !buttons.Test(button)) throw InvalidParameters("Question window did not offer this button");
		if (!witness.at("text_hash").is_string()) throw InvalidParameters("Invalid question text commitment");
		const auto &hash = witness.at("text_hash").get_ref<const std::string &>();
		if (hash.size() != 64 || !std::ranges::all_of(hash, [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); })) throw InvalidParameters("Invalid question text commitment");
		return {true, "", ""};
	} catch (const std::exception &error) {
		return {false, error.what(), "invalid_native_target"};
	}
}

std::string StoryPlacementVersion(StoryPageElementID id)
{
	RequireCompany();
	const StoryPageElement *element = StoryPageElement::GetIfValid(id);
	if (element == nullptr || (element->type != StoryPageElementType::ButtonTile && element->type != StoryPageElementType::ButtonVehicle)) throw InvalidParameters("Story placement button is unavailable");
	const StoryPage *page = PageFor(element);
	if (!PageInteractive(page)) throw InvalidParameters("Story placement button is unavailable to this company");
	return PageVersion(page, true);
}

std::optional<ActionResult> DispatchStoryAction(const std::string &action, const Json &parameters, ActionInvocation &invocation)
{
	if (action != "goal.question.answer" && action != "story.button.press" && action != "story.button.select_tile" && action != "story.button.select_vehicle") return std::nullopt;
	RequireCompany();
	Parameters p(parameters);
	if (action == "goal.question.answer") {
		const Question &question = ReadQuestion(parameters);
		auto button = ReadButton(p);
		RequireButton(question, button);
		return invocation.Post<Commands::GoalQuestionAnswer>(question.id, button);
	}
	StoryPageElementType type = action == "story.button.press" ? StoryPageElementType::ButtonPush : action == "story.button.select_tile" ? StoryPageElementType::ButtonTile : StoryPageElementType::ButtonVehicle;
	const StoryPageElement *element = InteractiveElement(parameters, type);
	TileIndex tile = type == StoryPageElementType::ButtonTile ? p.Tile("tile") : TileIndex{};
	VehicleID vehicle = type == StoryPageElementType::ButtonVehicle ? ReadSelectableVehicle(parameters, element)->index : VehicleID::Invalid();
	return invocation.Post<Commands::StoryPageButton>(tile, element->index, vehicle);
}

std::optional<Json> CaptureStoryTargets(const std::string &action, const Json &parameters)
{
	if (action == "goal.question.answer") {
		const Question &question = ReadQuestion(parameters);
		RequireButton(question, ReadButton(Parameters(parameters)));
		return Json{{"question_id", QuestionHandle(question)}, {"state", QuestionProof(question)}};
	}
	if (action != "story.button.press" && action != "story.button.select_tile" && action != "story.button.select_vehicle") return std::nullopt;
	StoryPageElementType type = action == "story.button.press" ? StoryPageElementType::ButtonPush : action == "story.button.select_tile" ? StoryPageElementType::ButtonTile : StoryPageElementType::ButtonVehicle;
	const StoryPageElement *element = InteractiveElement(parameters, type);
	const StoryPage *page = PageFor(element);
	Json result = {{"element_id", Handle(element)}, {"page_id", Handle(page)}, {"page_version", PageVersion(page, true)}};
	if (type == StoryPageElementType::ButtonVehicle) result["vehicle_id"] = Handle(ReadSelectableVehicle(parameters, element));
	if (type == StoryPageElementType::ButtonTile) result["tile"] = Parameters(parameters).Tile("tile").base();
	return result;
}

std::optional<Json> QueryStoryState(const std::string &query, const Json &parameters)
{
	if (query == "news.custom") {
		RequireCompany();
		return QueryCustomNews(parameters);
	}
	if (query != "goal.questions" && query != "goal.question" && query != "story.pages" && query != "story.elements" && query != "story.element" && query != "story.vehicle_candidates") return std::nullopt;
	RequireCompany();
	Parameters p(parameters);
	Json result = SnapshotBase();
	if (query == "goal.questions") {
		const int64_t after_id = p.Int("after_id", -1, UINT16_MAX, -1);
		uint64_t after_generation = UINT64_MAX; // Legacy after_id alone skips that entire ID.
		if (parameters.contains("after_generation")) {
			const auto &value = parameters.at("after_generation");
			if (!value.is_number_integer() || (!value.is_number_unsigned() && value.get<int64_t>() < 0)) throw InvalidParameters("Invalid question issuance cursor");
			after_generation = value.get<uint64_t>();
			if (after_id == -1 && after_generation != 0) throw InvalidParameters("Initial question cursor must have generation zero");
		}
		size_t limit = static_cast<size_t>(p.Int("limit", 1, 32, 32));
		uint scanned = 0;
		Json items = Json::array();
		size_t item_bytes = 0;
		int64_t cursor_id = after_id;
		uint64_t cursor_generation = after_generation;
		const auto recipient = *CompanyRecipient(_current_company);
		auto next = _questions.lower_bound(static_cast<uint16_t>(std::max<int64_t>(after_id, 0)));
		bool stopped = false;
		for (; next != _questions.end(); ++next) {
			if (++scanned > 4096) break;
			auto slot = next->second.find(recipient);
			if (slot != next->second.end()) {
				const auto &inbox = slot->second;
				auto question = next->first == after_id ? std::ranges::upper_bound(inbox, after_generation, {}, [](const auto &item) { return item->generation; }) : inbox.begin();
				for (; question != inbox.end(); ++question) {
					if (++scanned > 4096 || items.size() >= limit) { stopped = true; break; }
					Json item = QuestionMetadata(**question);
					size_t bytes = item.dump().size();
					if (!items.empty() && item_bytes + bytes > 32000) { stopped = true; break; }
					item_bytes += bytes;
					items.push_back(std::move(item));
					cursor_id = next->first;
					cursor_generation = (*question)->generation;
				}
				if (stopped) break;
			}
			cursor_id = next->first;
			cursor_generation = slot == next->second.end() || slot->second.empty() ? 0 : slot->second.back()->generation;
		}
		result["items"] = std::move(items);
		result["next_after_id"] = next != _questions.end() ? Json(cursor_id) : Json(nullptr);
		result["next_after_generation"] = next != _questions.end() ? Json(cursor_generation) : Json(nullptr);
	} else if (query == "goal.question") {
		const Question &question = ReadQuestion(parameters);
		result.update(QuestionMetadata(question));
		result.update(TextChunk(question.text.GetDecodedString(), p));
	} else if (query == "story.pages") {
		CompanyID book = _current_company;
		if (parameters.contains("book_company_id")) book = parameters.at("book_company_id").is_null() ? CompanyID::Invalid() : CompanyID(static_cast<uint8_t>(p.Int("book_company_id", 0, MAX_COMPANIES - 1)));
		if (book != CompanyID::Invalid() && !Company::IsValidID(book)) throw InvalidParameters("Storybook company is unavailable");
		result.update(StoryPoolPage<StoryPage>(p, [book](const StoryPage *page) { return page->company == CompanyID::Invalid() || page->company == book; }, PageMetadata));
	} else if (query == "story.elements") {
		const StoryPage *page = ReadPage(parameters);
		result.update(PageMetadata(page));
		result["page_version"] = PageVersion(page, true);
		result["title_text"] = TextChunk(page->title.GetDecodedString(), p);
		result.update(StoryPoolPage<StoryPageElement>(p, [page](const StoryPageElement *element) { return element->page == page->index; }, ElementMetadata));
	} else if (query == "story.element") {
		const StoryPageElement *element = ReadHandle<StoryPageElement>(parameters, "element_id");
		const StoryPage *page = PageFor(element);
		result.update(ElementMetadata(element));
		result["page_id"] = Handle(page);
		result["page_version"] = PageVersion(page, true);
		const Goal *goal = element->type == StoryPageElementType::Goal && element->referenced_id < 64000 ? Goal::GetIfValid(element->referenced_id) : nullptr;
		result.update(TextChunk(goal == nullptr ? element->text.GetDecodedString() : goal->text.GetDecodedString(), p));
	} else {
		const StoryPageElement *element = InteractiveElement(parameters, StoryPageElementType::ButtonVehicle);
		result["page_version"] = PageVersion(PageFor(element), true);
		result.update(StoryPoolPage<Vehicle>(p, [element](const Vehicle *vehicle) {
			if (!IsCompanyBuildableVehicleType(vehicle) || !vehicle->IsPrimaryVehicle()) return false;
			auto type = StoryPageButtonData{element->referenced_id}.GetVehicleType();
			return type == VehicleType::Invalid || type == vehicle->type;
		}, [](const Vehicle *vehicle) {
			TileIndex location = vehicle->tile;
			if (vehicle->type == VehicleType::Aircraft) location = TileXY(Clamp(vehicle->x_pos / static_cast<int>(TILE_SIZE), 0, static_cast<int>(Map::MaxX())), Clamp(vehicle->y_pos / static_cast<int>(TILE_SIZE), 0, static_cast<int>(Map::MaxY())));
			return Json{{"vehicle_id", Handle(vehicle)}, {"owner_company_id", vehicle->owner.base()}, {"vehicle_type", VehicleTypeName(vehicle->type)},
				{"name", Preview(GetString(STR_VEHICLE_NAME, vehicle->index))}, {"location", TileCoordinates(location)}};
		}));
	}
	if (query == "goal.question" || query == "story.element") {
		result["text_truncated"] = result.at("text_offset") != 0 || !result.at("next_text_offset").is_null();
	}
	if (result.dump().size() > 48000) throw InvalidParameters("Story report exceeds the observation byte budget");
	return result;
}

std::optional<Json> StoryIntentState(Commands command, const CommandDataBuffer &data, ClientID owner)
{
	if (command == Commands::GoalQuestionAnswer) {
		auto [id, button] = Decode<Commands::GoalQuestionAnswer>(data);
		if (!RendererIsActive()) throw InvalidParameters("Native question window witness cannot be reconstructed from the text inbox");
		RequireQuestionOwner(owner);
		auto witness = RendererQuestionCommandWitness(id, button);
		if (!witness.has_value()) throw InvalidParameters("Question command has no matching clicked native popup");
		return *witness;
	}
	if (command != Commands::StoryPageButton) return std::nullopt;
	const Vehicle *vehicle = nullptr;
	const StoryPageElement *element = NativeElement(data, &vehicle);
	Json result = {{"element_id", element->index.base()}, {"page_id", element->page.base()}, {"page_context", PageVersion(PageFor(element), false)}};
	if (vehicle != nullptr) result["selection"] = {{"vehicle_id", vehicle->index.base()}, {"vehicle_type", to_underlying(vehicle->type)}, {"primary", true}};
	return result;
}

std::optional<ActionResult> ValidateStoryNativeTargets(Commands command, const CommandDataBuffer &data, const TargetWatermarks &watermarks, ClientID owner)
{
	if (command != Commands::GoalQuestionAnswer && command != Commands::StoryPageButton) return std::nullopt;
	try {
		if (command == Commands::GoalQuestionAnswer) {
			auto [id, button] = Decode<Commands::GoalQuestionAnswer>(data);
			RequireQuestionOwner(owner);
			if (button >= GoalQuestionButton::End) throw InvalidParameters("Invalid native question button");
		} else {
			const Vehicle *vehicle = nullptr;
			const StoryPageElement *element = NativeElement(data, &vehicle);
			const StoryPage *page = PageFor(element);
			if (element->GetInstanceGeneration() > watermarks.story_element || page->GetInstanceGeneration() > watermarks.story_page) throw InvalidParameters("Story context was created after the source frame");
			if (vehicle != nullptr && vehicle->GetInstanceGeneration() > watermarks.vehicle) throw InvalidParameters("Selected vehicle was created after the source frame");
			/* Referenced goals can be replaced while the page/element survives. */
			for (const StoryPageElement *part : StoryPageElement::Iterate()) {
				if (part->page != page->index) continue;
				if (part->GetInstanceGeneration() > watermarks.story_element) throw InvalidParameters("Story context was extended after the source frame");
				if (part->type != StoryPageElementType::Goal || part->referenced_id >= 64000) continue;
				const Goal *goal = Goal::GetIfValid(part->referenced_id);
				if (goal != nullptr && goal->GetInstanceGeneration() > watermarks.goal) throw InvalidParameters("Story goal was created after the source frame");
			}
		}
		return ActionResult{true, "", ""};
	} catch (const std::exception &error) {
		return ActionResult{false, error.what(), "invalid_target"};
	}
}

} // namespace Detail
} // namespace Integration
