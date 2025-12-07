import os
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import sys
import random
import asyncio

from telegram import Update, ReplyKeyboardMarkup
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

LECTURE_SOON_TEMPLATES = [
    "{course} starts in {minutes} minutes. Location: {room}.",
    "Heads up! {course} in {minutes} min. Room: {room}.",
    "{course} begins at {time}. Room {room}.",
    "Your {course} is starting in {minutes}. Location: {room}.",
    "{course} in {minutes} minutes at {room}.",
]

LEAVING_NOW_TEMPLATES = [
    "Your first lecture starts in {minutes} min. Take bus {bus_line} at {bus_depart}. {weather_action}. {weather_details}",
    "First lecture in {minutes} minutes! Catch bus {bus_line} departing {bus_depart}. {weather_action} - {weather_details}",
    "Time to head out! Bus {bus_line} leaves at {bus_depart}. {weather_action}, {weather_details}",
    "Your first lecture is coming up in {minutes} min. Take bus {bus_line} at {bus_depart}. {weather_action}",
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
        ["Mukkulankatu (M19)"],
        ["Niemenkatu (NIE73)"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Which campus do you want to go to?",
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
        return ASKING_BUS_DESTINATION
    
    # Plan route to destination
    dest = CAMPUSES[destination_campus]
    now = datetime.now()
    arrival_time = now + timedelta(minutes=30)
    
    api_key = get_api_key()
    itineraries = plan_route(
        config["home_lat"], config["home_lon"],
        dest["lat"], dest["lon"],
        arrival_time,
        api_key,
        num_results=1
    )
    
    if not itineraries:
        await update.message.reply_text(f"No buses available to reach {dest['name']}.")
        return
    
    best = itineraries[0]
    depart_time = format_time(best.get("start", ""))
    
    # Get weather
    departure_dt = datetime.fromisoformat(best.get("start", "").replace("Z", "+00:00"))
    if departure_dt.tzinfo:
        departure_dt = departure_dt.replace(tzinfo=None)
    weather_action, weather_details = get_weather_info(config["home_lat"], config["home_lon"], departure_dt)
    
    bus_legs = [leg for leg in best.get("legs", []) if leg.get("mode") == "BUS"]
    
    if bus_legs:
        leg = bus_legs[0]
        route = leg.get("trip", {}).get("routeShortName", "?")
        headsign = leg.get("trip", {}).get("tripHeadsign", "")
        from_stop = leg.get("from", {}).get("stop", {}).get("name", "")
        
        message = (
            f"Next bus: {route} → {headsign}\n"
            f"From: {from_stop}\n"
            f"Departs: {depart_time}\n"
            f"{weather_action}, {weather_details}"
        )
    else:
        message = f"Walk to {dest['name']}"
    
    keyboard = [["My Schedule", "Find Bus Now"], ["Settings"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(message, reply_markup=reply_markup)
    
    return ConversationHandler.END


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
        num_results=3
    )
    
    best = itineraries[0]
    depart_time = format_time(best.get("start", ""))
    
    bus_legs = [leg for leg in best.get("legs", []) if leg.get("mode") == "BUS"]
    if bus_legs:
        leg = bus_legs[0]
        route = leg.get("trip", {}).get("routeShortName", "?")
        headsign = leg.get("trip", {}).get("tripHeadsign", "")
        from_stop = leg.get("from", {}).get("stop", {}).get("name", "")
        message = f"Take bus {route} → {headsign}\nFrom: {from_stop}\nDeparts: {depart_time}"
    else:
        message = "Walk to destination"
    
    keyboard = [["My Schedule", "Find Bus Now"], ["Settings"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(message, reply_markup=reply_markup)
    
    return ConversationHandler.END


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Show settings
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    
    message = "*Current Settings:*\n\n"
    message += f"Home: {config.get('home_address', 'Not set')}\n"
    message += f"TimeEdit: {'Configured' if config.get('timeedit_url') else 'Not set'}\n"
    
    keyboard = [["Reset Settings"], ["Back"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")


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
        await settings(update, context)
    elif text == "Help":
        await help_command(update, context)
    elif text == "Reset Settings":
        user_id = update.effective_user.id
        USER_DATA_DIR = SCRIPT_DIR / "users"
        config_file = USER_DATA_DIR / f"{user_id}_config.json"
        if config_file.exists():
            config_file.unlink()
        keyboard = [["OK"]]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Settings reset. Use /start to reconfigure.", reply_markup=reply_markup)
    elif text == "Back":
        keyboard = [
            ["My Schedule", "Find Bus Now"],
            ["Settings", "Help"]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Menu:", reply_markup=reply_markup)
    elif text == "OK":
        keyboard = [
            ["My Schedule", "Find Bus Now"],
            ["Settings", "Help"]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Menu:", reply_markup=reply_markup)


async def check_and_send_notifications(app: Application):
    # Background task to check schedule and send notifications
    while True:
        await asyncio.sleep(60)  # Check every minute
        
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
                                message = template.format(
                                    minutes=int(minutes_until),
                                    bus_line=route,
                                    bus_depart=depart_time,
                                    weather_action=weather_action,
                                    weather_details=weather_details
                                )
                                await app.bot.send_message(user_id, message)
                
                # OTHER LECTURES - 14-16 minutes before
                elif 14 <= minutes_until <= 16:
                    template = random.choice(LECTURE_SOON_TEMPLATES)
                    message = template.format(
                        course=course,
                        room=room or "Unknown",
                        time=event_start.strftime("%H:%M"),
                        minutes=int(minutes_until)
                    )
                    await app.bot.send_message(user_id, message)


def main():
    # Start the bot
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Setup conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & filters.Regex("^Find Bus Now$"), find_bus_now),
        ],
        states={
            SETUP_TIMEEDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_timeedit)],
            SETUP_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_address)],
            ASKING_BUS_DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_campus_selection)],
            ASKING_CAMPUS_FOR_BUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bus_destination)],
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
