"""
Smart Bus Finder for Lahti - LUT University
============================================
This system helps you find the best bus to reach your lectures/exercise sessions at LUT University.

DESTINATIONS:
- Mukkulankatu 19, Lahti (M19 - Mukkulankatu campus)
- Niemenkatu 73, Lahti (NIE73 - Niemenkatu campus)

TIMEEDIT INTEGRATION:
1. Go to your TimeEdit schedule
2. Click "Subscribe" (top right)
3. Select "Current week + 12 months" for the iCal subscription
4. Copy the iCal link (starts with https://cloud.timeedit.net/...)
5. Paste it when prompted OR save it in config.json

The system will:
- Automatically fetch your schedule from TimeEdit
- Analyze room/location info (M19_xxx or NIE73_xxx) to determine which campus
- Learn and remember course locations over time
- Refresh the schedule daily to catch any changes

SETUP:
1. Register at https://portal-api.digitransit.fi/ to get an API key
2. Add your API key to the .env file: DIGITRANSIT_API_KEY=your_key_here
3. Run this script and follow the setup prompts
4. Your settings will be saved in config.json

TO RESET ALL DATA:
Delete these files: config.json, course_locations.json, schedule_cache.json

USAGE:
Run this script and it will automatically find your next lecture!
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict
import re
from pathlib import Path
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
import sys

# ==============================================================================
# FILE PATHS
# ==============================================================================

SCRIPT_DIR = Path(__file__).parent
PARENT_DIR = SCRIPT_DIR.parent

# Add parent directory to path to import weather module
sys.path.insert(0, str(PARENT_DIR))
from weather import get_weather_forecast, check_rain_at_time, interpret_weather

# .env file is in the parent directory
ENV_FILE = PARENT_DIR / ".env"
CONFIG_FILE = SCRIPT_DIR / "config.json"
COURSE_LOCATIONS_FILE = SCRIPT_DIR / "course_locations.json"
SCHEDULE_CACHE_FILE = SCRIPT_DIR / "schedule_cache.json"

# Load environment variables from .env file
load_dotenv(ENV_FILE)

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Digitransit API endpoint for Waltti regions (includes Lahti)
DIGITRANSIT_API_URL = "https://api.digitransit.fi/routing/v2/waltti/gtfs/v1"

# Geocoding API for address to coordinates
GEOCODING_API_URL = "https://api.digitransit.fi/geocoding/v1/search"

# Campus locations in Lahti (coordinates)
# Using correct coordinates near the campus bus stops
CAMPUSES = {
    "mukkulankatu": {
        "name": "Mukkulankatu Campus (M19)",
        "address": "Mukkulankatu 19, Lahti",
        # Correct coordinates - nearest bus stop: Niemen kampus P
        "lat": 61.00474,
        "lon": 25.665574,
        # Room prefixes/patterns that indicate this campus
        # TimeEdit uses M19_xxx format for Mukkulankatu rooms
        "room_patterns": ["M19_", "M19", "MUK"]
    },
    "niemenkatu": {
        "name": "Niemenkatu Campus (NIE73)", 
        "address": "Niemenkatu 73, Lahti",
        # Correct coordinates - nearest bus stop: Laatikkotehtaankatu P
        "lat": 61.005842,
        "lon": 25.654641,
        # Room prefixes/patterns that indicate this campus
        # TimeEdit uses NIE73_xxx format for Niemenkatu rooms
        "room_patterns": ["NIE73_", "NIE73", "NI_", "NI73"]
    }
}

# Lectures in TimeEdit show :00 but actually start at :15
# We want to arrive at :05-:10 (5-10 min before actual start)
# So we ADD 5 minutes to the displayed time to get target arrival
LECTURE_ACTUAL_START_OFFSET = 15  # Lectures start 15 min after displayed time
ARRIVAL_BEFORE_ACTUAL_START = 10  # Arrive 10 min before actual lecture start

# How often to refresh schedule from TimeEdit (in hours)
SCHEDULE_REFRESH_HOURS = 20

# ==============================================================================
# CONFIGURATION MANAGEMENT
# ==============================================================================

def load_config() -> Dict:
    # Load configuration from file.
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(config: Dict):
    # Save configuration to file.
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

def load_course_locations() -> Dict[str, str]:
    # Load learned course locations from file.
    if COURSE_LOCATIONS_FILE.exists():
        with open(COURSE_LOCATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_course_locations(locations: Dict[str, str]):
   # Save learned course locations to file."""
    with open(COURSE_LOCATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(locations, f, indent=2, ensure_ascii=False)

def load_schedule_cache() -> Dict:
    # Load cached schedule data.
    if SCHEDULE_CACHE_FILE.exists():
        with open(SCHEDULE_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_schedule_cache(cache: Dict):
    # Save schedule cache to file.
    with open(SCHEDULE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, default=str, ensure_ascii=False)

# ==============================================================================
# API KEY MANAGEMENT
# ==============================================================================

def get_api_key(config: Dict) -> str:
    # Get Digitransit API key from config or prompt user.
    api_key = config.get("digitransit_api_key") or os.environ.get("DIGITRANSIT_API_KEY")
    
    if not api_key:
        print("\n" + "="*60)
        print("🔑 DIGITRANSIT API KEY REQUIRED")
        print("="*60)
        print("\nTo use this service, you need a free API key.")
        print("Register at: https://portal-api.digitransit.fi/")
        print("\nAfter registration:")
        print("1. Go to Products -> Subscribe to 'Digitransit developer API'")
        print("2. Go to Profile -> Copy your API key\n")
        
        api_key = input("Enter your API key: ").strip()
        
        if not api_key:
            raise ValueError("API key is required to use Digitransit services")
        
        # Save for future use
        config["digitransit_api_key"] = api_key
        save_config(config)
        print("✅ API key saved!")
    
    return api_key

# ==============================================================================
# TIMEEDIT CALENDAR PARSING
# ==============================================================================

# Finland timezone
FINLAND_TZ = ZoneInfo("Europe/Helsinki")

def parse_ics_datetime(dt_str: str) -> datetime:
    # Parse iCalendar datetime format to Finnish local time.
    # TimeEdit sends times in UTC (with Z suffix), so we convert to Finnish time.
    
    is_utc = "Z" in dt_str
    
    if ":" in dt_str:
        dt_str = dt_str.split(":")[-1]
    
    # Remove UTC indicator for parsing
    dt_str = dt_str.replace("Z", "")
    
    # Parse the datetime
    if "T" in dt_str:
        dt = datetime.strptime(dt_str, "%Y%m%dT%H%M%S")
    else:
        dt = datetime.strptime(dt_str, "%Y%m%d")
    
    # If it was UTC, convert to Finnish time
    if is_utc:
        dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(FINLAND_TZ)
        dt = dt.replace(tzinfo=None)  # Return naive datetime in Finnish time
    
    return dt

def parse_timeedit_ics(ics_content: str) -> List[Dict]:
    # Parse TimeEdit iCalendar content and extract events.
    events = []
    current_event = None
    current_field = None
    current_value = ""
    
    # Handle line continuations (lines starting with space/tab are continuations)
    lines = []
    for line in ics_content.split("\n"):
        line = line.rstrip("\r")
        if line.startswith(" ") or line.startswith("\t"):
            if lines:
                lines[-1] += line[1:]  # Append to previous line
        else:
            lines.append(line)
    
    for line in lines:
        if line == "BEGIN:VEVENT":
            current_event = {}
        elif line == "END:VEVENT":
            if current_event and "start" in current_event:
                events.append(current_event)
            current_event = None
        elif current_event is not None:
            if ":" in line:
                field, value = line.split(":", 1)
                # Handle fields with parameters (e.g., DTSTART;TZID=...)
                field_name = field.split(";")[0]
                
                if field_name == "SUMMARY":
                    current_event["summary"] = value
                elif field_name == "DTSTART":
                    try:
                        current_event["start"] = parse_ics_datetime(value)
                    except:
                        pass
                elif field_name == "DTEND":
                    try:
                        current_event["end"] = parse_ics_datetime(value)
                    except:
                        pass
                elif field_name == "LOCATION":
                    current_event["location"] = value
                elif field_name == "DESCRIPTION":
                    current_event["description"] = value
                elif field_name == "UID":
                    current_event["uid"] = value
    
    return events

def extract_course_name(event: Dict) -> str:
    # Extract a normalized course name from event for tracking purposes.
    summary = event.get("summary", "")
    
    # Clean up the summary - remove any backslashes (escape characters), trailing whitespace, newlines
    summary = summary.replace("\\", "").rstrip(" \n\r")
    
    # TimeEdit often has format: "Course Name, Room"
    # Try to extract just the course name part
    parts = summary.split(",")
    if parts:
        course_name = parts[0].strip()
        # Remove common prefixes like course codes
        # But keep enough to identify the course
        return course_name
    
    return summary.strip()

def extract_room_info(event: Dict) -> str:
    # Extract room/location info from event.
    # TimeEdit puts room info in LOCATION or sometimes in SUMMARY
    location = event.get("location", "")
    summary = event.get("summary", "")
    
    # Clean up summary and location - remove backslashes (escape characters)
    summary = summary.replace("\\", "").rstrip(" \n\r")
    location = location.replace("\\", "").rstrip(" \n\r")
    
    # First check for M19 or NIE73 patterns in summary (TimeEdit format)
    # Pattern like: M19_xxx or NIE73_xxx
    m19_match = re.search(r'M19_\w+', summary)
    if m19_match:
        return m19_match.group()
    
    nie73_match = re.search(r'NIE73_\w+', summary)
    if nie73_match:
        return nie73_match.group()
    
    # Check for any room pattern like M19_xxx or NI_xxx
    room_match = re.search(r'[MN]\d{2}_\w+', summary)
    if room_match:
        return room_match.group()
    
    if location:
        return location.strip()
    
    # Try to find room in summary (often after comma or semicolon)
    for separator in [",", ";", "|"]:
        if separator in summary:
            parts = summary.split(separator)
            if len(parts) > 1:
                room = parts[-1].strip()
                # Look for room-like patterns
                if room and (re.match(r'^[A-Z]{1,3}\d', room) or "room" in room.lower() or "sali" in room.lower() or "oppimistila" in room.lower()):
                    return room
    
    return ""

def analyze_room_for_campus(room_info: str, event: Optional[Dict] = None) -> Optional[str]:
    # Analyze room info and event summary to determine which campus.
    # Combine room info with summary for better detection
    search_text = room_info.upper()
    if event:
        search_text += " " + event.get("summary", "").upper()
    
    # Check for M19 pattern (Mukkulankatu 19)
    if "M19_" in search_text or "M19 " in search_text or search_text.endswith("M19"):
        return "mukkulankatu"
    
    # Check for NIE73 pattern (Niemenkatu 73)
    if "NIE73" in search_text or "NI73" in search_text:
        return "niemenkatu"
    
    # Check each campus's room patterns
    for campus_key, campus_data in CAMPUSES.items():
        for pattern in campus_data.get("room_patterns", []):
            if pattern.upper() in search_text:
                return campus_key
    
    # Check for address keywords
    search_lower = search_text.lower()
    if "mukkula" in search_lower or "mukkulankatu" in search_lower:
        return "mukkulankatu"
    if "niemen" in search_lower or "niemenkatu" in search_lower:
        return "niemenkatu"
    
    return None

def fetch_timeedit_schedule(url: str, force_refresh: bool = False) -> List[Dict]:
    # Fetch schedule from TimeEdit, using cache if recent.
    cache = load_schedule_cache()
    
    # Check if cache is still valid
    if not force_refresh and cache.get("url") == url:
        last_fetch = cache.get("last_fetch")
        if last_fetch:
            last_fetch_time = datetime.fromisoformat(last_fetch)
            if datetime.now() - last_fetch_time < timedelta(hours=SCHEDULE_REFRESH_HOURS):
                print(f"📅 Using cached schedule (last updated: {last_fetch_time.strftime('%Y-%m-%d %H:%M')})")
                # Convert stored events back to proper format
                events = []
                for e in cache.get("events", []):
                    event = dict(e)
                    if "start" in event:
                        event["start"] = datetime.fromisoformat(event["start"])
                    if "end" in event:
                        event["end"] = datetime.fromisoformat(event["end"])
                    events.append(event)
                return events
    
    # Fetch fresh data
    print("📅 Fetching schedule from TimeEdit...")
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        events = parse_timeedit_ics(response.text)
        
        # Update cache
        cache_events = []
        for e in events:
            cache_event = dict(e)
            if "start" in cache_event:
                cache_event["start"] = cache_event["start"].isoformat()
            if "end" in cache_event:
                cache_event["end"] = cache_event["end"].isoformat()
            cache_events.append(cache_event)
        
        cache = {
            "url": url,
            "last_fetch": datetime.now().isoformat(),
            "events": cache_events
        }
        save_schedule_cache(cache)
        
        print(f"   ✅ Found {len(events)} events")
        return events
        
    except Exception as e:
        print(f"   ⚠️  Error fetching schedule: {e}")
        # Return cached events if available
        if cache.get("events"):
            print("   Using previously cached schedule")
            events = []
            for e in cache.get("events", []):
                event = dict(e)
                if "start" in event:
                    event["start"] = datetime.fromisoformat(event["start"])
                if "end" in event:
                    event["end"] = datetime.fromisoformat(event["end"])
                events.append(event)
            return events
        return []

def learn_and_determine_campus(event: Dict, course_locations: Dict[str, str]) -> Tuple[Optional[str], bool]:
    """
    Determine campus for an event, learning from room info.
    Returns (campus_key, was_learned) tuple.
    
    Priority:
    1. Room info from current event (M19_xxx or NIE73_xxx) - most reliable
    2. Previously learned course location (if no room info available)
    3. Description hints
    """
    course_name = extract_course_name(event)
    room_info = extract_room_info(event)
    
    # FIRST: Try to determine from room info AND event summary
    # This is the most reliable source - room codes tell us exactly where the class is
    campus_from_room = analyze_room_for_campus(room_info, event)
    
    if campus_from_room:
        # Update stored location if different (room info overrides stored data)
        if course_locations.get(course_name) != campus_from_room:
            course_locations[course_name] = campus_from_room
            save_course_locations(course_locations)
            return campus_from_room, True
        return campus_from_room, False
    
    # SECOND: Check if we already know this course's location (fallback when no room info)
    if course_name in course_locations:
        return course_locations[course_name], False
    
    # THIRD: Check description for any hints
    description = event.get("description", "").lower()
    if "mukkulankatu" in description or "mukkula" in description or "m19" in description.lower():
        course_locations[course_name] = "mukkulankatu"
        save_course_locations(course_locations)
        return "mukkulankatu", True
    if "niemenkatu" in description or "niemen" in description or "nie73" in description.lower():
        course_locations[course_name] = "niemenkatu"
        save_course_locations(course_locations)
        return "niemenkatu", True
    
    return None, False

# ==============================================================================
# SCHEDULE ANALYSIS
# ==============================================================================

def get_next_lecture(events: List[Dict], from_time: Optional[datetime] = None) -> Optional[Dict]:
    # Find the next lecture/event from the schedule.
    if from_time is None:
        from_time = datetime.now()
    
    # Filter future events and sort by start time
    future_events = [
        e for e in events 
        if "start" in e and e["start"] > from_time
    ]
    
    if not future_events:
        return None
    
    future_events.sort(key=lambda x: x["start"])
    return future_events[0]

def get_todays_lectures(events: List[Dict]) -> List[Dict]:
    # Get all lectures for today.
    today = datetime.now().date()
    
    todays_events = [
        e for e in events
        if "start" in e and e["start"].date() == today
    ]
    
    todays_events.sort(key=lambda x: x["start"])
    return todays_events

def print_schedule_summary(events: List[Dict], course_locations: Dict[str, str]):
    # Print a summary of today's and tomorrow's lectures.
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    
    print("\n" + "="*60)
    print("📅 SCHEDULE SUMMARY")
    print("="*60)
    
    for day, day_name in [(today, "TODAY"), (tomorrow, "TOMORROW")]:
        day_events = [e for e in events if "start" in e and e["start"].date() == day]
        day_events.sort(key=lambda x: x["start"])
        
        if day_events:
            print(f"\n📆 {day_name} ({day.strftime('%A, %B %d')}):")
            for event in day_events:
                time_str = event["start"].strftime("%H:%M")
                course = extract_course_name(event)
                room = extract_room_info(event)
                
                # Determine campus from room info (priority) or stored location
                campus_key = analyze_room_for_campus(room, event)
                if not campus_key:
                    campus_key = course_locations.get(course)
                
                campus_icon = "🏫" if campus_key else "❓"
                if campus_key:
                    campus_name = CAMPUSES[campus_key]["name"].split("(")[1].replace(")", "")
                else:
                    campus_name = "Unknown"
                
                print(f"   {time_str} - {course}")
                if room:
                    print(f"           📍 {room} ({campus_name}) {campus_icon}")
        else:
            print(f"\n📆 {day_name}: No lectures scheduled")

# ==============================================================================
# GEOCODING
# ==============================================================================

def geocode_address(address: str, api_key: str) -> Optional[Tuple[float, float]]:
    # Convert address to coordinates using Digitransit Geocoding API.
    params = {
        "text": address,
        "size": 1,
        "boundary.rect.min_lat": 60.9,
        "boundary.rect.max_lat": 61.1,
        "boundary.rect.min_lon": 25.4,
        "boundary.rect.max_lon": 25.9
    }
    
    headers = {
        "digitransit-subscription-key": api_key
    }
    
    try:
        response = requests.get(GEOCODING_API_URL, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        if data.get("features"):
            coords = data["features"][0]["geometry"]["coordinates"]
            return (coords[1], coords[0])
    except Exception as e:
        print(f"⚠️  Geocoding error: {e}")
    
    return None

# ==============================================================================
# ROUTE PLANNING
# ==============================================================================

def plan_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    arrive_by: datetime = None,
    api_key: str = None,
    num_results: int = 5,
    depart_after: datetime = None
) -> Optional[List[Dict]]:
    """
    Plan a route using Digitransit Routing API (Waltti).
    
    The API determines the optimal routes - we don't hardcode any routing logic.
    We use preferences to:
    - Prefer buses over walking (high walk reluctance)
    - Prefer fewer transfers (high board cost)
    - Allow the API to find the best combination
    
    Args:
        depart_after: Find buses departing after this time (preferred for "find bus now")
        arrive_by: Find routes arriving by this time (for schedule-based planning)
    """
    
    # Make datetime timezone-aware for proper API formatting
    if depart_after:
        time_param = depart_after.replace(tzinfo=FINLAND_TZ)
        time_str = time_param.isoformat()
        datetime_clause = f'dateTime: {{earliestDeparture: "{time_str}"}}'
    elif arrive_by:
        time_param = arrive_by.replace(tzinfo=FINLAND_TZ)
        time_str = time_param.isoformat()
        datetime_clause = f'dateTime: {{latestArrival: "{time_str}"}}'
    else:
        # Default: use current time as earliest departure
        now = datetime.now().replace(tzinfo=FINLAND_TZ)
        time_str = now.isoformat()
        datetime_clause = f'dateTime: {{earliestDeparture: "{time_str}"}}'
    
    # GraphQL query with preferences to favor transit over walking
    # - walkReluctance: Higher value = prefer transit over walking (default 2.0, we use 3.0)
    # - boardCost: Cost in seconds for boarding, prefer fewer transfers (5 min = 300 sec)
    # - walkSpeed: Average walking speed 1.33 m/s (about 4.8 km/h)
    # Let the Waltti API determine the best routes - no hardcoded routing
    query = """
    {{
      planConnection(
        origin: {{location: {{coordinate: {{latitude: {}, longitude: {}}}}}}}
        destination: {{location: {{coordinate: {{latitude: {}, longitude: {}}}}}}}
        first: {}
        {}
        modes: {{
          transit: {{transit: [{{mode: BUS}}]}}
        }}
        preferences: {{
          street: {{
            walk: {{
              speed: 1.33
              reluctance: 3.0
              boardCost: 300
            }}
          }}
          transit: {{
            transfer: {{
              slack: "2M"
            }}
          }}
        }}
      ) {{
        edges {{
          node {{
            start
            end
            duration
            walkDistance
            numberOfTransfers
            legs {{
              mode
              from {{
                name
                stop {{
                  name
                  code
                }}
              }}
              to {{
                name
                stop {{
                  name
                  code
                }}
              }}
              start {{
                scheduledTime
                estimated {{
                  time
                }}
              }}
              end {{
                scheduledTime
                estimated {{
                  time
                }}
              }}
              trip {{
                routeShortName
                tripHeadsign
              }}
              distance
              duration
            }}
          }}
        }}
      }}
    }}
    """.format(origin_lat, origin_lon, dest_lat, dest_lon, num_results, datetime_clause)
    
    headers = {
        "Content-Type": "application/graphql",
        "digitransit-subscription-key": api_key
    }
    
    try:
        response = requests.post(DIGITRANSIT_API_URL, data=query, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        if "errors" in data:
            print(f"⚠️  API Error: {data['errors']}")
            return None
        
        edges = data.get("data", {}).get("planConnection", {}).get("edges", [])
        itineraries = [edge["node"] for edge in edges]
        
        # Sort by: shorter duration first, then fewer transfers
        # This prioritizes fastest routes while still preferring fewer transfers as tiebreaker
        itineraries.sort(key=lambda x: (x.get("duration", 0), x.get("numberOfTransfers", 0)))
        
        return itineraries
    
    except Exception as e:
        print(f"⚠️  Route planning error: {e}")
        return None

def format_time(iso_time: str) -> str:
    # Format ISO time string to readable format.
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except:
        return iso_time

def format_duration(seconds: int) -> str:
    # Format duration in seconds to readable format.
    minutes = seconds // 60
    if minutes >= 60:
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}min"
    return f"{minutes} min"

def count_bus_legs(itinerary: Dict) -> int:
    # Count number of bus legs in an itinerary.
    return sum(1 for leg in itinerary.get("legs", []) if leg.get("mode") == "BUS")

def get_transfers(itinerary: Dict) -> int:
    # Get number of transfers from itinerary (API provides this directly).
    return itinerary.get("numberOfTransfers", count_bus_legs(itinerary) - 1)

def print_itinerary(itinerary: Dict, index: int):
    # Print a formatted itinerary.
    print(f"\n{'='*60}")
    
    # Use API's transfer count (more reliable)
    transfers = get_transfers(itinerary)
    bus_count = count_bus_legs(itinerary)
    
    if transfers > 0:
        print(f"🚌 OPTION {index + 1} ({transfers} transfer{'s' if transfers > 1 else ''})")
    else:
        print(f"🚌 OPTION {index + 1} (direct)")
    print("="*60)
    
    start_time = format_time(itinerary.get("start", ""))
    end_time = format_time(itinerary.get("end", ""))
    duration = format_duration(itinerary.get("duration", 0))
    walk_dist = itinerary.get("walkDistance", 0)
    
    print(f"⏰ Departure: {start_time}  →  Arrival: {end_time}")
    print(f"⏱️  Total duration: {duration}")
    print(f"🚶 Total walking: {walk_dist:.0f} m")
    if transfers > 0:
        print(f"🔄 Transfers: {transfers}")
    print(f"\n📍 Route details:")
    print("-" * 40)
    
    legs = itinerary.get("legs", [])
    bus_number = 0
    
    for i, leg in enumerate(legs):
        mode = leg.get("mode", "")
        from_name = leg.get("from", {}).get("name", "Unknown")
        to_name = leg.get("to", {}).get("name", "Unknown")
        
        start = leg.get("start", {})
        leg_start = format_time(start.get("scheduledTime", ""))
        
        end = leg.get("end", {})
        leg_end = format_time(end.get("scheduledTime", ""))
        
        leg_duration = format_duration(leg.get("duration", 0))
        
        if mode == "WALK":
            distance = leg.get("distance", 0)
            is_first = (i == 0)
            is_last = (i == len(legs) - 1)
            
            # Check if this walk is between two buses (transfer)
            is_transfer = False
            if not is_first and not is_last:
                prev_leg = legs[i - 1] if i > 0 else None
                next_leg = legs[i + 1] if i < len(legs) - 1 else None
                if prev_leg and next_leg:
                    is_transfer = prev_leg.get("mode") == "BUS" and next_leg.get("mode") == "BUS"
            
            if is_first:
                print(f"  🚶 WALK to bus stop ({leg_duration}, {distance:.0f} m)")
            elif is_last:
                print(f"  🚶 WALK to destination ({leg_duration}, {distance:.0f} m)")
            elif is_transfer:
                print(f"  🔄 TRANSFER WALK ({leg_duration}, {distance:.0f} m)")
            else:
                print(f"  🚶 WALK ({leg_duration}, {distance:.0f} m)")
            print(f"     {from_name} → {to_name}")
        elif mode == "BUS":
            bus_number += 1
            trip = leg.get("trip", {})
            route = trip.get("routeShortName", "?")
            headsign = trip.get("tripHeadsign", "")
            from_stop = leg.get("from", {}).get("stop", {})
            to_stop = leg.get("to", {}).get("stop", {})
            from_code = from_stop.get("code", "") if from_stop else ""
            to_code = to_stop.get("code", "") if to_stop else ""
            
            bus_label = f"BUS {bus_number}" if bus_count > 1 else "BUS"
            print(f"  🚌 {bus_label}: Line {route} → {headsign}")
            print(f"     Board at {leg_start}: {from_name}" + (f" [{from_code}]" if from_code else ""))
            print(f"     Exit at {leg_end}: {to_name}" + (f" [{to_code}]" if to_code else ""))
        else:
            print(f"  {mode}: {from_name} → {to_name} ({leg_duration})")
        print()

# ==============================================================================
# SETUP AND USER INTERACTION
# ==============================================================================

def setup_timeedit_url(config: Dict) -> str:
    # Get or set up TimeEdit URL.
    url = config.get("timeedit_url")
    
    if url:
        print(f"\n📅 TimeEdit URL configured: {url[:50]}...")
        change = input("   Change URL? (y/N): ").strip().lower()
        if change != "y":
            return url
    
    print("\n" + "="*60)
    print("📅 TIMEEDIT SETUP")
    print("="*60)
    print("\nTo get your TimeEdit iCal subscription link:")
    print("1. Go to your TimeEdit schedule")
    print("2. Click 'Subscribe' (top right)")
    print("3. Select 'Current week + 12 months' for full semester view")
    print("4. Copy the iCal link\n")
    print("The link should look like:")
    print("https://cloud.timeedit.net/lut-saimia/web/...")
    print()
    
    while True:
        url = input("Paste your TimeEdit iCal link: ").strip()
        
        if not url:
            print("⚠️  URL is required")
            continue
        
        if "timeedit" not in url.lower() and ".ics" not in url.lower():
            print("⚠️  This doesn't look like a TimeEdit link. Continue anyway? (y/N): ", end="")
            if input().strip().lower() != "y":
                continue
        
        # Test the URL
        print("\n🔍 Testing connection...")
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            if "VCALENDAR" in response.text:
                print("✅ Successfully connected to TimeEdit!")
                config["timeedit_url"] = url
                save_config(config)
                return url
            else:
                print("⚠️  URL works but doesn't seem to be a calendar. Try anyway? (y/N): ", end="")
                if input().strip().lower() == "y":
                    config["timeedit_url"] = url
                    save_config(config)
                    return url
        except Exception as e:
            print(f"❌ Could not connect: {e}")
            print("   Check the URL and try again")

def setup_home_location(config: Dict, api_key: str) -> Tuple[float, float]:
    # Get or set up home location.
    if config.get("home_lat") and config.get("home_lon"):
        addr = config.get("home_address", "Saved location")
        print(f"\n🏠 Home location: {addr}")
        change = input("   Change location? (y/N): ").strip().lower()
        if change != "y":
            return config["home_lat"], config["home_lon"]
    
    print("\n" + "="*60)
    print("🏠 HOME LOCATION SETUP")
    print("="*60)
    print("\nEnter your home address in Lahti.")
    print("Examples:")
    print("  - Vapaudenkatu 24, Lahti")
    print("  - Aleksanterinkatu 18, Lahti\n")
    
    while True:
        address = input("Your address: ").strip()
        
        if not address:
            print("⚠️  Please enter an address")
            continue
        
        if "lahti" not in address.lower():
            address += ", Lahti"
        
        print(f"\n🔍 Looking up: {address}")
        coords = geocode_address(address, api_key)
        
        if coords:
            print(f"✅ Found location!")
            config["home_address"] = address
            config["home_lat"] = coords[0]
            config["home_lon"] = coords[1]
            save_config(config)
            return coords
        else:
            print("❌ Could not find that address. Please try again.")

def select_destination_manually() -> str:
    # Let user manually select destination.
    print("\n" + "="*60)
    print("🏫 SELECT DESTINATION")
    print("="*60)
    print("\n1. Mukkulankatu 19 (Mukkulankatu Campus)")
    print("2. Niemenkatu 73 (Niemenkatu Campus)")
    
    while True:
        choice = input("\nSelect destination (1/2): ").strip()
        if choice == "1":
            return "mukkulankatu"
        elif choice == "2":
            return "niemenkatu"
        print("Please enter 1 or 2")

def teach_course_location(event: Dict, course_locations: Dict[str, str]) -> str:
    # Ask user to teach the system which campus a course is at.
    course_name = extract_course_name(event)
    
    print(f"\n❓ I don't know where '{course_name}' is held.")
    print("   Please tell me which campus:")
    print("   1. Mukkulankatu 19 (Mukkulankatu Campus)")
    print("   2. Niemenkatu 73 (Niemenkatu Campus)")
    
    while True:
        choice = input("\n   Campus (1/2): ").strip()
        if choice == "1":
            campus = "mukkulankatu"
            break
        elif choice == "2":
            campus = "niemenkatu"
            break
        print("   Please enter 1 or 2")
    
    # Save for future
    course_locations[course_name] = campus
    save_course_locations(course_locations)
    print(f"   ✅ Got it! I'll remember '{course_name}' is at {CAMPUSES[campus]['name']}")
    
    return campus

# ==============================================================================
# MAIN APPLICATION
# ==============================================================================

def main():
    # Main application
    print("\n" + "="*60)
    print("🚌 SMART BUS FINDER FOR LAHTI")
    print("   LUT University - TimeEdit Integration")
    print("="*60)
    
    # Load configuration
    config = load_config()
    course_locations = load_course_locations()
    
    # Setup API key
    try:
        api_key = get_api_key(config)
    except ValueError as e:
        print(f"\n❌ {e}")
        return
    
    # Setup TimeEdit URL
    timeedit_url = setup_timeedit_url(config)
    
    # Setup home location
    origin_lat, origin_lon = setup_home_location(config, api_key)
    
    # Fetch schedule from TimeEdit
    events = fetch_timeedit_schedule(timeedit_url)
    
    if not events:
        print("\n❌ No events found in your TimeEdit schedule.")
        print("   Check your TimeEdit subscription link.")
        return
    
    # Analyze all events to learn locations
    print("\n🔍 Analyzing schedule for campus locations...")
    learned_count = 0
    for event in events:
        campus, was_learned = learn_and_determine_campus(event, course_locations)
        if was_learned:
            learned_count += 1
            course = extract_course_name(event)
            print(f"   📚 Learned: '{course}' → {CAMPUSES[campus]['name']}")
    
    if learned_count > 0:
        print(f"   ✅ Learned {learned_count} new course location(s)")
    
    # Show schedule summary
    print_schedule_summary(events, course_locations)
    
    # Find next lecture
    next_event = get_next_lecture(events)
    
    if not next_event:
        print("\n✨ No upcoming lectures! Enjoy your free time!")
        return
    
    # Display next lecture info
    print("\n" + "="*60)
    print("📚 NEXT LECTURE")
    print("="*60)
    
    course_name = extract_course_name(next_event)
    room_info = extract_room_info(next_event)
    start_time = next_event["start"]
    
    print(f"\n   📖 {course_name}")
    print(f"   ⏰ {start_time.strftime('%A, %B %d at %H:%M')}")
    if room_info:
        print(f"   📍 Room: {room_info}")
    
    # Determine destination
    destination_campus, _ = learn_and_determine_campus(next_event, course_locations)
    
    if not destination_campus:
        destination_campus = teach_course_location(next_event, course_locations)
    
    dest = CAMPUSES[destination_campus]
    print(f"   🏫 Campus: {dest['name']}")
    print(f"   📫 Address: {dest['address']}")
    
    # Calculate arrival time
    # Lectures show :00 in TimeEdit but actually start at :15
    # For a lecture showing 08:00 (actual start 08:15), we want to arrive at ~08:05
    actual_lecture_start = start_time + timedelta(minutes=LECTURE_ACTUAL_START_OFFSET)
    arrival_time = actual_lecture_start - timedelta(minutes=ARRIVAL_BEFORE_ACTUAL_START)
    print(f"\n⏰ Target arrival: {arrival_time.strftime('%H:%M')} (lecture starts at {actual_lecture_start.strftime('%H:%M')})")
    print(f"   🎯 Destination: {dest['address']}")
    
    # Show time until lecture
    time_until = start_time - datetime.now()
    if time_until.days > 0:
        print(f"📆 Time until lecture: {time_until.days} days, {time_until.seconds // 3600} hours")
    else:
        hours = time_until.seconds // 3600
        minutes = (time_until.seconds % 3600) // 60
        print(f"⏳ Time until lecture: {hours}h {minutes}min")
    
    # Plan the route
    print("\n🔍 Finding the best bus routes...")
    
    itineraries = plan_route(
        origin_lat, origin_lon,
        dest["lat"], dest["lon"],
        arrival_time,
        api_key
    )
    
    if not itineraries:
        print("\n❌ No bus routes found. This could mean:")
        print("   - No buses available at this time")
        print("   - The destination is close enough to walk!")
        print("   - Try running again closer to departure time")
        return
    
    print(f"\n✅ Found {len(itineraries)} route option(s):")
    
    for i, itinerary in enumerate(itineraries):
        print_itinerary(itinerary, i)
    
    # Recommend the best option
    print("\n" + "="*60)
    print("💡 RECOMMENDATION")
    print("="*60)
    
    if itineraries:
        # Best option is first (already sorted by duration, then transfers)
        best = itineraries[0]
        start_time_str = format_time(best.get("start", ""))
        end_time_str = format_time(best.get("end", ""))
        transfers = get_transfers(best)
        
        # Get departure time for weather check
        try:
            departure_dt = datetime.fromisoformat(best.get("start", "").replace("Z", "+00:00"))
            # Make it naive for comparison with weather data
            if departure_dt.tzinfo:
                departure_dt = departure_dt.replace(tzinfo=None)
        except:
            departure_dt = datetime.now()
        
        # Collect all bus legs
        bus_legs = [leg for leg in best.get("legs", []) if leg.get("mode") == "BUS"]
        
        if len(bus_legs) == 0:
            print("\n🚶 Walk to destination (no bus needed)")
            print(f"   Estimated arrival: {end_time_str}")
        elif transfers == 0:
            # Direct bus - simple recommendation
            leg = bus_legs[0]
            trip = leg.get("trip", {})
            route = trip.get("routeShortName", "?")
            headsign = trip.get("tripHeadsign", "")
            from_stop = leg.get("from", {}).get("stop", {})
            to_stop = leg.get("to", {}).get("stop", {})
            stop_name = from_stop.get("name", leg.get("from", {}).get("name", "")) if from_stop else leg.get("from", {}).get("name", "")
            exit_stop = to_stop.get("name", leg.get("to", {}).get("name", "")) if to_stop else leg.get("to", {}).get("name", "")
            bus_time = format_time(leg.get("start", {}).get("scheduledTime", ""))
            
            print(f"\n✅ DIRECT route available!")
            print(f"\n🚌 Take bus {route} → {headsign}")
            print(f"⏰ Departs: {bus_time}")
            print(f"📍 Board at: {stop_name}")
            print(f"🏁 Exit at: {exit_stop}")
            print(f"🎯 Arrives: {end_time_str}")
        else:
            # Multiple buses - show transfer info
            print(f"\n⚠️  Best route requires {transfers} transfer(s):")
            
            for i, leg in enumerate(bus_legs):
                trip = leg.get("trip", {})
                route = trip.get("routeShortName", "?")
                headsign = trip.get("tripHeadsign", "")
                from_stop = leg.get("from", {}).get("stop", {})
                to_stop = leg.get("to", {}).get("stop", {})
                stop_name = from_stop.get("name", leg.get("from", {}).get("name", "")) if from_stop else leg.get("from", {}).get("name", "")
                exit_stop = to_stop.get("name", leg.get("to", {}).get("name", "")) if to_stop else leg.get("to", {}).get("name", "")
                bus_time = format_time(leg.get("start", {}).get("scheduledTime", ""))
                
                if i == 0:
                    print(f"\n   1️⃣ First bus:")
                else:
                    print(f"\n   {i+1}️⃣ Transfer to:")
                
                print(f"      🚌 Line {route} → {headsign}")
                print(f"      ⏰ Departs: {bus_time}")
                print(f"      📍 Board: {stop_name}")
                print(f"      🚏 Exit: {exit_stop}")
            
            print(f"\n🎯 Arrives: {end_time_str}")
        
        print(f"\n⏰ Leave home by: {start_time_str}")
        
        # Weather information
        print("\n" + "-"*60)
        print("🌦️  WEATHER AT DEPARTURE")
        print("-"*60)
        try:
            # Get weather for departure location (home) at departure time
            forecast = get_weather_forecast(origin_lat, origin_lon)
            weather_result = check_rain_at_time(departure_dt, forecast)
            
            prob = weather_result["probability"]
            prec = weather_result["precipitation"]
            
            if weather_result["rain"]:
                # Rain expected - warn user
                if prec >= 2.0:
                    print("\n☔ Heavy rain expected! Don't forget your umbrella and rain gear!")
                elif prec >= 0.5:
                    print("\n🌧️  Moderate rain expected. Take an umbrella!")
                else:
                    print("\n🌦️  Light rain possible. Consider bringing an umbrella.")
            else:
                # No significant rain expected
                if prob == 0:
                    print("\n☁️  No rain expected at departure time.")
                elif prob < 20:
                    print("\n☁️  Very low chance of rain. Umbrella likely not needed.")
                else:
                    print("\n☁️  Low chance of rain. You should be fine without an umbrella.")
            
            print(f"   Rain probability: {prob}%")
            if prec > 0:
                print(f"   Expected precipitation: {prec:.1f} mm")
        except Exception as e:
            print(f"\n⚠️  Could not fetch weather data: {e}")

if __name__ == "__main__":
    main()
