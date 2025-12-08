import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import sys
import random
import asyncio

from telegram import Update, ReplyKeyboardMarkup
from telegram.helpers import escape_markdown
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler


SCRIPT_DIR = Path(__file__).parent
PARENT_DIR = SCRIPT_DIR.parent
TRANSPORT_DIR = PARENT_DIR / "transport"
sys.path.insert(0, str(PARENT_DIR))
sys.path.insert(0, str(TRANSPORT_DIR))

from weather import get_weather_forecast, check_rain_at_time
from transport import (
    load_course_locations, save_course_locations, fetch_timeedit_schedule,
    learn_and_determine_campus, get_todays_lectures,
    extract_course_name, extract_room_info, geocode_address, plan_route, format_time,
    CAMPUSES, LECTURE_ACTUAL_START_OFFSET, ARRIVAL_BEFORE_ACTUAL_START
)

ENV_FILE = PARENT_DIR / ".env"
load_dotenv(ENV_FILE)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
USER_DATA_DIR = PARENT_DIR / "users"
USER_DATA_DIR.mkdir(exist_ok=True)

# Conversation states
SETUP_TIMEEDIT, SETUP_ADDRESS = range(2)
ASKING_BUS_DESTINATION = 100
ASKING_CAMPUS_FOR_BUS = 101
CHANGE_TIMEEDIT = 102
CHANGE_ADDRESS = 103
ASKING_DIRECTION = 104

LECTURE_SOON_TEMPLATES = [
    "Heads up! {course} in {minutes} mins at {room}. Stop procrastinating!",
    "Yo! {course} starts in {minutes} mins. Get to {room}!",
    "Your {course} is basically happening now ({minutes} mins). Room: {room}",
    "Time flies! {course} in {minutes} minutes at {room}. Move it!",
    "Wake up! {course} starts at {time} in {room} ({minutes} mins to go)",
    "Incoming! {course} at {time}. That's {minutes} mins. Location: {room}",
]

LEAVING_NOW_TEMPLATES = [
    "Yo, wake up! First class in {minutes} mins. Catch bus {bus_line} at {bus_depart}. {weather_action} btw - {weather_details}",
    "Let's go! Bus {bus_line} leaves {bus_depart}. You got {minutes} mins. {weather_action}, {weather_details}",
    "Rise and shine! Bus {bus_line} at {bus_depart} (in {minutes} mins). {weather_action} - {weather_details}",
    "MOVE! First lecture in {minutes} mins. Grab the {bus_line} at {bus_depart}. {weather_action}, {weather_details}",
    "Heads up! Bus {bus_line} at {bus_depart}. Your class is in {minutes} mins. {weather_action}, {weather_details}",
]

AFTER_LAST_TEMPLATE = "Your last lecture ends at {end_time}. Take bus {bus_line} heading {direction} at {dep_time}. {weather_action} - {weather_details}"


def get_user_config(user_id: int) -> dict:
    # Load user-specific config
    config_file = USER_DATA_DIR / f"{user_id}_config.json"
    if config_file.exists():
        with open(config_file, "r") as f:
            return json.load(f)
    return {}


def save_user_config(user_id: int, config: dict):
    # Save user-specific config
    config_file = USER_DATA_DIR / f"{user_id}_config.json"
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)


def get_api_key():
    # Get Digitransit API key from environment
    return os.environ.get("DIGITRANSIT_API_KEY", "")


def get_weather_info(lat: float, lon: float, departure_time: datetime) -> tuple:
    # Extract weather action and details for the given time and location
    forecast = get_weather_forecast(lat, lon)
    weather_result = check_rain_at_time(departure_time, forecast)
    
    if weather_result["rain"]:
        action = "take an umbrella"
        details = "Heavy rain expected" if weather_result["precipitation"] >= 2.0 else "Light rain expected"
    else:
        action = "no umbrella needed"
        details = "Clear weather"
    
    return action, details


def format_route_message(itinerary: dict, config: dict, departure_dt: datetime) -> str:
    """Format a route itinerary into a detailed message similar to transport.py"""
    start_time_str = format_time(itinerary.get("start", ""))
    end_time_str = format_time(itinerary.get("end", ""))
    
    # Get weather info
    weather_action, weather_details = get_weather_info(
        config["home_lat"], config["home_lon"], departure_dt
    )
    
    # Collect all bus legs
    bus_legs = [leg for leg in itinerary.get("legs", []) if leg.get("mode") == "BUS"]
    
    if len(bus_legs) == 0:
        return f"🚶 Walk to destination\n🎯 Arrives: {end_time_str}"
    
    message = ""
    
    if len(bus_legs) == 1:
        # Direct bus - simple format
        leg = bus_legs[0]
        trip = leg.get("trip", {})
        route = trip.get("routeShortName", "?")
        headsign = trip.get("tripHeadsign", "")
        from_stop = leg.get("from", {}).get("stop", {})
        to_stop = leg.get("to", {}).get("stop", {})
        stop_name = from_stop.get("name", "") or leg.get("from", {}).get("name", "")
        exit_stop = to_stop.get("name", "") or leg.get("to", {}).get("name", "")
        bus_time = format_time(leg.get("start", {}).get("scheduledTime", ""))
        
        message = f"✅ DIRECT route available!\n\n"
        message += f"🚌 Take bus {route} → {headsign}\n"
        message += f"⏰ Departs: {bus_time}\n"
        message += f"📍 Board at: {stop_name}\n"
        message += f"🏁 Exit at: {exit_stop}\n"
        message += f"🎯 Arrives: {end_time_str}\n\n"
        message += f"⏰ Leave home by: {start_time_str}\n\n"
        message += f"🌦️ {weather_action}, {weather_details}"
    else:
        # Multiple buses - show transfers
        transfers = len(bus_legs) - 1
        message = f"⚠️ Best route requires {transfers} transfer(s):\n\n"
        
        for i, leg in enumerate(bus_legs):
            trip = leg.get("trip", {})
            route = trip.get("routeShortName", "?")
            headsign = trip.get("tripHeadsign", "")
            from_stop = leg.get("from", {}).get("stop", {})
            to_stop = leg.get("to", {}).get("stop", {})
            stop_name = from_stop.get("name", "") or leg.get("from", {}).get("name", "")
            exit_stop = to_stop.get("name", "") or leg.get("to", {}).get("name", "")
            bus_time = format_time(leg.get("start", {}).get("scheduledTime", ""))
            
            if i == 0:
                message += f"1️⃣ First bus:\n"
            else:
                message += f"\n{i+1}️⃣ Transfer to:\n"
            
            message += f"   🚌 Line {route} → {headsign}\n"
            message += f"   ⏰ Departs: {bus_time}\n"
            message += f"   📍 Board: {stop_name}\n"
            message += f"   🚏 Exit: {exit_stop}\n"
        
        message += f"\n🎯 Arrives: {end_time_str}\n"
        message += f"⏰ Leave home by: {start_time_str}\n\n"
        message += f"🌦️ {weather_action}, {weather_details}"
    
    return message


def format_notification_message(itinerary: dict, minutes_until: int, config: dict, departure_dt: datetime) -> str:
    """Format a notification message for first lecture with detailed bus info"""
    weather_action, weather_details = get_weather_info(
        config["home_lat"], config["home_lon"], departure_dt
    )
    
    bus_legs = [leg for leg in itinerary.get("legs", []) if leg.get("mode") == "BUS"]
    
    if not bus_legs:
        return f"⏰ First class in {int(minutes_until)} mins. Time to leave!"
    
    if len(bus_legs) == 1:
        # Single bus
        leg = bus_legs[0]
        route = leg.get("trip", {}).get("routeShortName", "?")
        headsign = leg.get("trip", {}).get("tripHeadsign", "")
        stop_name = leg.get("from", {}).get("stop", {}).get("name", "") or leg.get("from", {}).get("name", "")
        bus_time = format_time(leg.get("start", {}).get("scheduledTime", ""))
        
        message = f"⏰ First class in {int(minutes_until)} mins!\n\n"
        message += f"🚌 Take bus {route} → {headsign}\n"
        message += f"📍 Board at: {stop_name}\n"
        message += f"⏰ Departs: {bus_time}\n\n"
        message += f"🌦️ {weather_action}, {weather_details}"
    else:
        # Multiple buses
        first_leg = bus_legs[0]
        route = first_leg.get("trip", {}).get("routeShortName", "?")
        headsign = first_leg.get("trip", {}).get("tripHeadsign", "")
        stop_name = first_leg.get("from", {}).get("stop", {}).get("name", "") or first_leg.get("from", {}).get("name", "")
        bus_time = format_time(first_leg.get("start", {}).get("scheduledTime", ""))
        
        message = f"⏰ First class in {int(minutes_until)} mins!\n"
        message += f"⚠️ Requires {len(bus_legs)-1} transfer(s)\n\n"
        message += f"🚌 First bus: {route} → {headsign}\n"
        message += f"📍 Board at: {stop_name}\n"
        message += f"⏰ Departs: {bus_time}\n\n"
        message += f"🌦️ {weather_action}, {weather_details}"
    
    return message




async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Start command - check if user needs setup
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    
    if config.get("timeedit_url") and config.get("home_lat"):
        # Already set up
        keyboard = [
            ["My Schedule", "Find Bus Now"],
            ["Settings", "Help"]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            f"Welcome back! Use the menu below to get started.",
            reply_markup=reply_markup
        )
    else:
        # Need setup
        await update.message.reply_text(
            "Welcome to Brainbuddy!\n\n"
            "Let's set up your account. I'll need:\n"
            "1. Your TimeEdit iCal link\n"
            "2. Your home address in Lahti\n\n"
            "First, please share your TimeEdit iCal subscription link.\n\n"
            "How to get it:\n"
            "1. Go to your TimeEdit schedule\n"
            "2. Click 'Subscribe' (top right)\n"
            "3. Select 'Current week + 12 months'\n"
            "4. Copy the iCal link (starts with https://cloud.timeedit.net/...)"
        )
        return SETUP_TIMEEDIT


async def setup_timeedit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle TimeEdit URL input
    user_id = update.effective_user.id
    url = update.message.text.strip()
    
    # Validate URL format
    if "timeedit" not in url.lower() and ".ics" not in url.lower():
        await update.message.reply_text("That doesn't look like a TimeEdit link. Please try again.")
        return SETUP_TIMEEDIT
    
    # Save and move to next step
    config = get_user_config(user_id)
    config["timeedit_url"] = url
    save_user_config(user_id, config)
    
    await update.message.reply_text(
        "TimeEdit link saved!\n\nNow, what's your home address in Lahti?\nExample: Vapaudenkatu 20, Lahti"
    )
    return SETUP_ADDRESS


async def setup_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle home address input
    user_id = update.effective_user.id
    address = update.message.text.strip()
    
    if "lahti" not in address.lower():
        address += ", Lahti"
    
    # Geocode the address
    api_key = get_api_key()
    coords = geocode_address(address, api_key)
    
    # Save config
    config = get_user_config(user_id)
    config["home_address"] = address
    config["home_lat"] = coords[0]
    config["home_lon"] = coords[1]
    save_user_config(user_id, config)
    
    # Setup complete
    keyboard = [
        ["My Schedule", "Find Bus Now"],
        ["Settings", "Help"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"Setup complete!\n\nHome: {address}",
        reply_markup=reply_markup
    )
    
    return ConversationHandler.END


async def show_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Show today's schedule
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    
    if not config.get("timeedit_url"):
        await update.message.reply_text("Please complete setup first. Use /start")
        return
    
    # Fetch schedule
    events = fetch_timeedit_schedule(config["timeedit_url"])
    todays = get_todays_lectures(events)
    
    if not todays:
        await update.message.reply_text("No lectures today!")
        return
    
    message = "*Today's Schedule:*\n\n"
    for event in todays:
        course = extract_course_name(event)
        room = extract_room_info(event)
        start = event["start"].strftime("%H:%M")
        
        # Escape special characters for Markdown
        course = escape_markdown(course, version=1)
        if room:
            room = escape_markdown(room, version=1)

        message += f"{start} - {course}"
        if room:
            message += f"\n    {room}"
        message += "\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")


async def find_bus_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Find first suitable bus leaving right now
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    
    if not config.get("home_lat"):
        await update.message.reply_text("Please complete setup first. Use /start")
        return
    
    keyboard = [
        ["To Campus"],
        ["From Campus"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Where are you going?",
        reply_markup=reply_markup
    )
    return ASKING_DIRECTION


async def handle_direction_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle direction selection (to/from campus)
    text = update.message.text.strip()
    
    if text == "To Campus":
        context.user_data["direction"] = "to_campus"
    elif text == "From Campus":
        context.user_data["direction"] = "from_campus"
    else:
        await update.message.reply_text("Please select To Campus or From Campus")
        return ASKING_DIRECTION
    
    keyboard = [
        ["Mukkulankatu (M19)"],
        ["Niemenkatu (NIE73)"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Which campus?",
        reply_markup=reply_markup
    )
    return ASKING_CAMPUS_FOR_BUS


async def handle_bus_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle bus destination selection
    user_id = update.effective_user.id
    text = update.message.text.strip()
    config = get_user_config(user_id)
    
    if "Mukkulankatu" in text or "M19" in text:
        destination_campus = "mukkulankatu"
    elif "Niemenkatu" in text or "NIE73" in text:
        destination_campus = "niemenkatu"
    else:
        await update.message.reply_text("Please select Mukkulankatu or Niemenkatu")
        return ASKING_CAMPUS_FOR_BUS
    
    # Get direction from context
    direction = context.user_data.get("direction", "to_campus")
    dest = CAMPUSES[destination_campus]
    now = datetime.now()
    api_key = get_api_key()
    
    # Plan route based on direction
    if direction == "to_campus":
        # From home to campus
        itineraries = plan_route(
            config["home_lat"], config["home_lon"],
            dest["lat"], dest["lon"],
            depart_after=now,
            api_key=api_key,
            num_results=1
        )
    else:
        # From campus to home
        itineraries = plan_route(
            dest["lat"], dest["lon"],
            config["home_lat"], config["home_lon"],
            depart_after=now,
            api_key=api_key,
            num_results=1
        )
    
    if not itineraries:
        keyboard = [["My Schedule", "Find Bus Now"], ["Settings"]]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        destination_name = "home" if direction == "from_campus" else dest['name']
        await update.message.reply_text(
            f"No buses available to reach {destination_name}.",
            reply_markup=reply_markup
        )
        return ConversationHandler.END
    
    best = itineraries[0]
    
    # Get departure time for weather
    departure_dt = datetime.fromisoformat(best.get("start", "").replace("Z", "+00:00"))
    if departure_dt.tzinfo:
        departure_dt = departure_dt.replace(tzinfo=None)
    
    # Use appropriate coordinates for weather (departure location)
    if direction == "to_campus":
        weather_lat, weather_lon = config["home_lat"], config["home_lon"]
    else:
        weather_lat, weather_lon = dest["lat"], dest["lon"]
    
    # Create a temporary config for weather lookup
    temp_config = {"home_lat": weather_lat, "home_lon": weather_lon}
    
    # Format detailed message
    message = format_route_message(best, temp_config, departure_dt)
    
    keyboard = [["My Schedule", "Find Bus Now"], ["Settings"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(message, reply_markup=reply_markup)
    
    return ConversationHandler.END


# Update handle_campus_selection function (line 310)
async def handle_campus_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle campus selection when destination unknown
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if "Mukkulankatu" in text or "M19" in text:
        destination_campus = "mukkulankatu"
    elif "Niemenkatu" in text or "NIE73" in text:
        destination_campus = "niemenkatu"
    else:
        await update.message.reply_text("Please select Mukkulankatu or Niemenkatu")
        return ASKING_BUS_DESTINATION
    
    # Get event and plan route
    next_event = context.user_data.get("next_event")
    config = get_user_config(user_id)
    
    # Save learned location
    course_locations = load_course_locations()
    course_name = extract_course_name(next_event)
    course_locations[course_name] = destination_campus
    save_course_locations(course_locations)
    
    # Plan route
    dest = CAMPUSES[destination_campus]
    actual_start = next_event["start"] + timedelta(minutes=LECTURE_ACTUAL_START_OFFSET)
    arrival_time = actual_start - timedelta(minutes=ARRIVAL_BEFORE_ACTUAL_START)
    
    api_key = get_api_key()
    itineraries = plan_route(
        config["home_lat"], config["home_lon"],
        dest["lat"], dest["lon"],
        arrival_time,
        api_key,
        num_results=1
    )
    
    if not itineraries:
        await update.message.reply_text("No routes found.")
        return ConversationHandler.END
    
    best = itineraries[0]
    
    # Get departure time for weather
    departure_dt = datetime.fromisoformat(best.get("start", "").replace("Z", "+00:00"))
    if departure_dt.tzinfo:
        departure_dt = departure_dt.replace(tzinfo=None)
    
    # Format detailed message
    message = format_route_message(best, config, departure_dt)
    
    keyboard = [["My Schedule", "Find Bus Now"], ["Settings"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(message, reply_markup=reply_markup)
    
    return ConversationHandler.END



async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Show settings
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    
    home_address = config.get('home_address', 'Not set')
    home_address = escape_markdown(home_address, version=1)
    
    message = "*Current Settings:*\n\n"
    message += f"Home: {home_address}\n"
    message += f"TimeEdit: {'Configured' if config.get('timeedit_url') else 'Not set'}\n"
    
    keyboard = [["Change Address"], ["Change TimeEdit"], ["Back"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")


async def change_timeedit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Start changing TimeEdit link
    await update.message.reply_text(
        "Please paste your new TimeEdit iCal subscription link.\n\n"
        "How to get it:\n"
        "1. Go to your TimeEdit schedule\n"
        "2. Click 'Subscribe' (top right)\n"
        "3. Select 'Current week + 12 months'\n"
        "4. Copy the iCal link (starts with https://cloud.timeedit.net/...)"
    )
    return CHANGE_TIMEEDIT


async def handle_change_timeedit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle new TimeEdit URL input
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    # Check for Back button
    if text == "Back":
        user_id = update.effective_user.id
        config = get_user_config(user_id)
        
        home_address = config.get('home_address', 'Not set')
        home_address = escape_markdown(home_address, version=1)
        
        message = "*Current Settings:*\n\n"
        message += f"Home: {home_address}\n"
        message += f"TimeEdit: {'Configured' if config.get('timeedit_url') else 'Not set'}\n"
        
        keyboard = [["Change Address"], ["Change TimeEdit"], ["Back"]]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")
        return ConversationHandler.END
    
    # Validate URL format
    if "timeedit" not in text.lower() and ".ics" not in text.lower():
        await update.message.reply_text("That doesn't look like a TimeEdit link. Please try again.")
        return CHANGE_TIMEEDIT
    
    # Save new URL
    config = get_user_config(user_id)
    config["timeedit_url"] = text
    save_user_config(user_id, config)
    
    keyboard = [["My Schedule", "Find Bus Now"], ["Settings", "Help"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "✅ TimeEdit link updated!",
        reply_markup=reply_markup
    )
    
    return ConversationHandler.END


async def change_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Start changing address
    await update.message.reply_text(
        "Please paste your new home address in Lahti.\n"
        "Example: Vapaudenkatu 20, Lahti"
    )
    return CHANGE_ADDRESS


async def handle_change_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle new address input
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    # Check for Back button
    if text == "Back":
        config = get_user_config(user_id)
        
        home_address = config.get('home_address', 'Not set')
        home_address = escape_markdown(home_address, version=1)
        
        message = "*Current Settings:*\n\n"
        message += f"Home: {home_address}\n"
        message += f"TimeEdit: {'Configured' if config.get('timeedit_url') else 'Not set'}\n"
        
        keyboard = [["Change Address"], ["Change TimeEdit"], ["Back"]]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")
        return ConversationHandler.END
    
    address = text
    if "lahti" not in address.lower():
        address += ", Lahti"
    
    # Geocode the address
    api_key = get_api_key()
    coords = geocode_address(address, api_key)
    
    # Save config
    config = get_user_config(user_id)
    config["home_address"] = address
    config["home_lat"] = coords[0]
    config["home_lon"] = coords[1]
    save_user_config(user_id, config)
    
    keyboard = [["My Schedule", "Find Bus Now"], ["Settings", "Help"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"✅ Address updated to: {address}",
        reply_markup=reply_markup
    )
    
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Show help
    message = (
        "*Brainbuddy Help*\n\n"
        "*Commands:*\n"
        "*My Schedule* - View today's lectures\n"
        "*Find Bus Now* - Find the best bus for your next lecture\n"
        "*Settings* - Manage your preferences\n"
        "*Help* - Show this message\n\n"
        "*Features:*\n"
        "• Automatic schedule fetching from TimeEdit\n"
        "• Smart bus route planning\n"
        "• Weather-aware notifications\n"
        "• Campus location learning"
    )
    await update.message.reply_text(message, parse_mode="Markdown")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle text messages with buttons
    text = update.message.text
    
    if text == "My Schedule":
        await show_schedule(update, context)
    elif text == "Settings":
        return await settings(update, context)
    elif text == "Help":
        await help_command(update, context)
    elif text == "Change TimeEdit":
        return await change_timeedit(update, context)
    elif text == "Change Address":
        return await change_address(update, context)
    elif text == "Back":
        keyboard = [
            ["My Schedule", "Find Bus Now"],
            ["Settings", "Help"]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Menu:", reply_markup=reply_markup)


async def check_and_send_notifications(app: Application):
    # Background task to check schedule and send notifications
    sent_notifications = set()
    while True:
        await asyncio.sleep(300)  # Check every 5 minutes
        
        # Get all user configs
        user_files = list(USER_DATA_DIR.glob("*_config.json"))
        
        for config_file in user_files:
            user_id = int(config_file.stem.split("_")[0])
            
            with open(config_file, "r") as f:
                config = json.load(f)
            
            if not config.get("timeedit_url") or not config.get("home_lat"):
                continue
            
            # Fetch schedule
            events = fetch_timeedit_schedule(config["timeedit_url"])
            if not events:
                continue
            
            course_locations = load_course_locations()
            api_key = get_api_key()
            now = datetime.now()
            
            # Check each event for notifications
            for event in events:
                if "start" not in event:
                    continue
                
                event_start = event["start"]
                minutes_until = (event_start - now).total_seconds() / 60
                
                # Skip past events and events too far in future
                if minutes_until < 0 or minutes_until > 120:
                    continue
                
                course = extract_course_name(event)
                room = extract_room_info(event)
                
                # FIRST LECTURE - 25-35 minutes before
                if 25 <= minutes_until <= 35:
                    notif_key = f"{user_id}_{course}_{event_start}_early"
                    if notif_key in sent_notifications:
                        continue

                    destination_campus, _ = learn_and_determine_campus(event, course_locations)
                    
                    if destination_campus:
                        dest = CAMPUSES[destination_campus]
                        actual_start = event_start + timedelta(minutes=LECTURE_ACTUAL_START_OFFSET)
                        arrival_time = actual_start - timedelta(minutes=ARRIVAL_BEFORE_ACTUAL_START)
                        
                        itineraries = plan_route(
                            config["home_lat"], config["home_lon"],
                            dest["lat"], dest["lon"],
                            arrival_time,
                            api_key,
                            num_results=1
                        )
                        
                        if itineraries:
                            best = itineraries[0]
                            depart_time = format_time(best.get("start", ""))
                            departure_dt = datetime.fromisoformat(best.get("start", "").replace("Z", "+00:00"))
                            if departure_dt.tzinfo:
                                departure_dt = departure_dt.replace(tzinfo=None)
                            weather_action, weather_details = get_weather_info(config["home_lat"], config["home_lon"], departure_dt)
                            
                            bus_legs = [leg for leg in best.get("legs", []) if leg.get("mode") == "BUS"]
                            if bus_legs:
                                route = bus_legs[0].get("trip", {}).get("routeShortName", "?")
                                template = random.choice(LEAVING_NOW_TEMPLATES)
                                message = format_notification_message(best, minutes_until, config, departure_dt)
                                await app.bot.send_message(user_id, message)
                                sent_notifications.add(notif_key)
                
                # OTHER LECTURES - 14-16 minutes before
                elif 14 <= minutes_until <= 16:
                    notif_key = f"{user_id}_{course}_{event_start}_soon"
                    if notif_key in sent_notifications:
                        continue

                    template = random.choice(LECTURE_SOON_TEMPLATES)
                    message = template.format(
                        course=course,
                        room=room or "Unknown",
                        time=event_start.strftime("%H:%M"),
                        minutes=int(minutes_until)
                    )
                    await app.bot.send_message(user_id, message)
                    sent_notifications.add(notif_key)


def main():
    # Start the bot
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Setup conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & filters.Regex("^Find Bus Now$"), find_bus_now),
            MessageHandler(filters.TEXT & filters.Regex("^Settings$"), settings),
            MessageHandler(filters.TEXT & filters.Regex("^Change TimeEdit$"), change_timeedit),
            MessageHandler(filters.TEXT & filters.Regex("^Change Address$"), change_address),
        ],
        states={
            SETUP_TIMEEDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_timeedit)],
            SETUP_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_address)],
            ASKING_DIRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_direction_choice)],
            ASKING_BUS_DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_campus_selection)],
            ASKING_CAMPUS_FOR_BUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bus_destination)],
            CHANGE_TIMEEDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_change_timeedit)],
            CHANGE_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_change_address)],
        },
        fallbacks=[CommandHandler("start", start)],
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    print("Bot started!")
    
    # Start background notification checker as a separate task
    async def run_bot():
        async with app:
            await app.start()
            # Start notification checker in background
            asyncio.create_task(check_and_send_notifications(app))
            await app.updater.start_polling()
            # Keep running
            await asyncio.Event().wait()
    
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()