import requests
from datetime import datetime

# Fetches hourly precipitation data for the given latitude and longitude
# Returns a list of tuples: (datetime, precipitation_probability, precipitation_mm)
def get_weather_forecast(lat, lon):
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        "&hourly=precipitation_probability,precipitation"
        "&forecast_days=1"
    )

    response = requests.get(url)
    data = response.json()

    times = data["hourly"]["time"]
    probs = data["hourly"]["precipitation_probability"]
    rain = data["hourly"]["precipitation"]

    forecast = []
    for t, p, r in zip(times, probs, rain):
        forecast.append((datetime.fromisoformat(t), p, r))

    return forecast

# Finds the closest forecast entry to the target time
# Determines if rain is expected using simple rules (probability > 40% or precipitation > 0.1 mm)
# Returns a dictionary:
# {
#     "rain": True/False,
#     "probability": <percentage>,
#     "precipitation": <mm>,
#     "time_checked": <ISO timestamp>
# }
def check_rain_at_time(target_time, forecast):
    # Find the forecast hour closest to the given time
    closest_entry = min(
        forecast,
        key=lambda entry: abs(entry[0] - target_time)
    )

    time_checked, probability, precipitation = closest_entry

    # Smart rule to determine if rain matters
    rain_expected = probability > 40 or precipitation > 0.1

    # Return dictionary
    return {
        "rain": rain_expected,
        "probability": probability,
        "precipitation": precipitation,
        "time_checked": time_checked.isoformat()
    }

# Convert weather data into a human-readable English message
def interpret_weather(result):
    prob = result["probability"]
    prec = result["precipitation"]

    # Probability interpretation
    if prob < 20:
        prob_text = "very low chance of rain"
    elif prob < 40:
        prob_text = "small chance of rain"
    elif prob < 70:
        prob_text = "high chance of rain"
    else:
        prob_text = "almost certain rain"

    # Precipitation interpretation
    if prec < 0.1:
        prec_text = "no significant precipitation"
    elif prec < 0.5:
        prec_text = "light rain expected"
    elif prec < 2.0:
        prec_text = "moderate rain expected"
    else:
        prec_text = "heavy rain expected"

    # Compose message
    if result["rain"]:
        status = f"{prec_text} ({prob_text})"
    else:
        status = f"No rain expected ({prob_text})"

    return f"At {result['time_checked']}, {status}."

# Placeholder function to get the target time for rain prediction
def get_time():
    # Placeholder for user-specific walk/lecture time
    return datetime.now()


# The result
def get_weather_prediction():
    # Lahti campus coordinates
    lat, lon = 61.00639, 25.66350

    forecast = get_weather_forecast(lat, lon)
    walk_time = get_time()

    result = check_rain_at_time(walk_time, forecast)
    message = interpret_weather(result)

    return message
