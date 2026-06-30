import os
import re
import requests
import sys
from urllib.parse import parse_qsl, urlparse
from dotenv import load_dotenv


BASE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
    )
)

ENV_PATH = os.path.join(
    BASE_DIR,
    ".env",
)

load_dotenv(ENV_PATH)

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.services.gemini_key_manager import generate_content_with_rotation

WEATHER_SOURCE_LABEL = "Weather API"


def _drop_dead_local_proxy_env() -> None:
    proxy_names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    for name in proxy_names:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 9:
            os.environ.pop(name, None)


_drop_dead_local_proxy_env()

# -----------------------------------
# Keyword groups
# -----------------------------------

WEATHER_KEYWORDS = [
    "weather",
    "temperature",
    "rain",
    "forecast",
    "humidity",
    "climate",
    "hot",
    "cold",
    "wind",
]

NEWS_KEYWORDS = [
    "news",
    "latest",
    "headline",
    "breaking",
    "update",
    "recent",
    "today news",
]

AUDIO_KEYWORDS = [
    "speaker",
    "audio",
    "transcript",
    "uploaded audio",
    "conversation",
    "meeting",
    "person 1",
    "person 2",
    "who said",
    "said",
    "what did",
    "discussed",
    "was discussed",
    "in the audio",
]


def _ask_log(message: str) -> None:
    if os.getenv("DEBUG", "false").lower() != "true":
        return

    print(
        "[Ask Anything]\n"
        f"{message}",
        flush=True,
    )


def _weather_debug(label: str, value) -> None:
    if os.getenv("DEBUG", "false").lower() != "true":
        return

    if label == "Weather URL:":
        parsed = urlparse(str(value))
        params = []
        for key, param_value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in {"appid", "api_key", "apikey", "key"}:
                params.append(f"{key}=<redacted>")
            else:
                params.append(f"{key}={param_value}")
        value = (
            f"{parsed.path}"
            + (f" params: {', '.join(params)}" if params else "")
        )
        label = "Weather Request:"
    elif label == "Raw Response:":
        value = f"<hidden, {len(str(value))} characters>"
        label = "Response Body:"

    fallback_encoding = sys.stdout.encoding or "utf-8"
    safe_value = (
        str(value)
        .encode(fallback_encoding, errors="backslashreplace")
        .decode(fallback_encoding)
    )
    print(label, safe_value, flush=True)


def _weather_log(
    location: str,
    result: str,
) -> None:
    print(
        "[Ask Anything] Weather request\n"
        f"Location: {location}\n"
        f"Result: {result}",
        flush=True,
    )


def _weather_error(
    message: str,
    location: str,
) -> None:
    print(
        f"[Weather API] {message}\n"
        f"Location: {location}",
        flush=True,
    )


def _generate_gemini_text(prompt: str) -> str:
    response = generate_content_with_rotation(
        model=os.getenv("GEMINI_AUDIO_MODEL", "gemini-2.5-flash"),
        contents=prompt,
    )
    return (response.text or "").strip()


# -----------------------------------
# Question Type Detection
# -----------------------------------

def detect_question_type(
    question: str,
) -> str:
    """
    Detect what type of question
    user asked.

    Returns:
    - weather
    - news
    - transcript
    - general
    """

    question_lower = (
        question.lower().strip()
    )

    # -----------------------
    # Weather detection
    # -----------------------

    for keyword in WEATHER_KEYWORDS:
        if keyword in question_lower:
            return "weather"

    # -----------------------
    # News detection
    # -----------------------

    for keyword in NEWS_KEYWORDS:
        if keyword in question_lower:
            return "news"

    # -----------------------
    # Transcript detection
    # -----------------------

    for keyword in AUDIO_KEYWORDS:
        if keyword in question_lower:
            return "transcript"

    # -----------------------
    # Default fallback
    # -----------------------

    return "general"


# -----------------------------------
# Weather Helpers
# -----------------------------------

def extract_city_name(
    question: str,
) -> str:
    """
    Extract city name
    from weather question.
    """

    city = question.strip()
    city = re.sub(
        r"\btoday['’]?s?\b",
        " ",
        city,
        flags=re.IGNORECASE,
    )
    city = re.sub(r"[?!.]+", " ", city)
    city = re.sub(
        r"\b(what|is|the|current|weather|temperature|forecast|of|for|in)\b",
        " ",
        city,
        flags=re.IGNORECASE,
    )
    city = re.sub(r"\s+", " ", city).strip()

    if not city:
        city = "Kolhapur"

    return city.title()


def format_weather_answer(
    city: str,
    data: dict,
) -> str:
    temperature = data[
        "main"
    ]["temp"]

    feels_like = data[
        "main"
    ]["feels_like"]

    humidity = data[
        "main"
    ]["humidity"]

    condition = data[
        "weather"
    ][0]["description"]

    wind_speed = data[
        "wind"
    ]["speed"]

    return (
        f"Current weather "
        f"in {city}:\n\n"
        f"Temperature: "
        f"{temperature}Â°C\n"
        f"Feels Like: "
        f"{feels_like}Â°C\n"
        f"Condition: "
        f"{condition.title()}\n"
        f"Humidity: "
        f"{humidity}%\n"
        f"Wind Speed: "
        f"{wind_speed} m/s\n\n"
        f"Source: {WEATHER_SOURCE_LABEL}"
    )


# -----------------------------------
# Weather Handler
# -----------------------------------

def handle_weather_question(
    question: str,
) -> str:
    """
    Get live weather
    using OpenWeather API.
    """

    api_key = os.getenv(
        "WEATHER_API_KEY"
    )

    if not api_key:
        _weather_error(
            "API key not configured",
            "Unknown",
        )
        return (
            "Weather API key "
            "not configured."
        )

    city = extract_city_name(
        question
    )
    used_geocoding_fallback = False

    _weather_debug("Question:", question)
    _weather_debug("Extracted city:", city)

    try:

        url = (
            "https://api.openweathermap.org"
            "/data/2.5/weather"
        )

        response = None
        data = {}
        for query in (city, f"{city},IN"):
            params = {
                "q": query,
                "appid": api_key,
                "units": "metric",
            }

            response = requests.get(
                url,
                params=params,
                timeout=10,
            )

            _weather_debug("Weather URL:", response.url)
            _weather_debug("Status Code:", response.status_code)
            _weather_debug("Raw Response:", response.text)

            data = response.json()
            if response.status_code == 200:
                break

        if (
            response is None
            or
            response.status_code
            != 200
        ):
            geo_url = (
                "https://api.openweathermap.org"
                "/geo/1.0/direct"
            )
            geo_response = requests.get(
                geo_url,
                params={
                    "q": f"{city},IN",
                    "limit": 1,
                    "appid": api_key,
                },
                timeout=10,
            )

            _weather_debug("Weather URL:", geo_response.url)
            _weather_debug("Status Code:", geo_response.status_code)
            _weather_debug("Raw Response:", geo_response.text)

            geo_data = geo_response.json()
            if (
                geo_response.status_code
                == 200
                and
                geo_data
            ):
                place = geo_data[0]
                lat = place["lat"]
                lon = place["lon"]
                city = (
                    place.get("name")
                    or
                    city
                )
            else:
                nominatim_response = requests.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={
                        "q": f"{city}, Maharashtra, India",
                        "format": "json",
                        "limit": 1,
                    },
                    headers={
                        "User-Agent": "Capstone-Project-Weather-Lookup/1.0",
                    },
                    timeout=10,
                )

                _weather_debug("Weather URL:", nominatim_response.url)
                _weather_debug("Status Code:", nominatim_response.status_code)
                _weather_debug("Raw Response:", nominatim_response.text)

                nominatim_data = nominatim_response.json()
                if (
                    nominatim_response.status_code
                    != 200
                    or
                    not nominatim_data
                ):
                    _weather_error(
                        "City lookup failed",
                        city,
                    )
                    _weather_log(
                        city or "Unknown",
                        "Failed",
                    )
                    return (
                        f"Could not fetch "
                        f"weather for "
                        f"{city}."
                    )

                place = nominatim_data[0]
                lat = place["lat"]
                lon = place["lon"]

            used_geocoding_fallback = True

            response = requests.get(
                url,
                params={
                    "lat": lat,
                    "lon": lon,
                    "appid": api_key,
                    "units": "metric",
                },
                timeout=10,
            )

            _weather_debug("Weather URL:", response.url)
            _weather_debug("Status Code:", response.status_code)
            _weather_debug("Raw Response:", response.text)

            data = response.json()
            if response.status_code != 200:
                _weather_error(
                    "Weather lookup failed",
                    city,
                )
                _weather_log(
                    city or "Unknown",
                    "Failed",
                )
                return (
                    f"Could not fetch "
                    f"weather for "
                    f"{city}."
                )

        temperature = data[
            "main"
        ]["temp"]

        feels_like = data[
            "main"
        ]["feels_like"]

        humidity = data[
            "main"
        ]["humidity"]

        condition = data[
            "weather"
        ][0]["description"]

        wind_speed = data[
            "wind"
        ]["speed"]

        answer = (
            f"Current weather "
            f"in {city}:\n\n"
            f"Temperature: "
            f"{temperature}°C\n"
            f"Feels Like: "
            f"{feels_like}°C\n"
            f"Condition: "
            f"{condition.title()}\n"
            f"Humidity: "
            f"{humidity}%\n"
            f"Wind Speed: "
            f"{wind_speed} m/s\n\n"
            f"Source: {WEATHER_SOURCE_LABEL}"
        )

        _weather_log(
            city,
            (
                "Success (Geocoding fallback)"
                if used_geocoding_fallback
                else
                "Success"
            ),
        )

        return answer

    except Exception as e:

        _weather_error(
            f"Request failed: {e}",
            city or "Unknown",
        )
        _weather_log(
            city or "Unknown",
            "Failed",
        )

        return (
            "Unable to fetch "
            "weather right now."
        )


# -----------------------------------
# News Handler
# -----------------------------------

def extract_news_topic(
    question: str,
) -> str:
    topic = question.strip()
    topic = re.sub(r"[?!.]+", " ", topic)
    topic = re.sub(
        r"\b(latest|recent|today|todays|today's|breaking|top)\b",
        " ",
        topic,
        flags=re.IGNORECASE,
    )
    topic = re.sub(
        r"\b(news|headlines|updates|update|about|on|for)\b",
        " ",
        topic,
        flags=re.IGNORECASE,
    )
    topic = re.sub(r"\s+", " ", topic).strip()
    if not topic:
        return "technology"
    if topic.upper() in {"AI", "ML", "UK", "US", "USA"}:
        return topic.upper()
    return topic.title()


def handle_news_question(
    question: str,
) -> str:
    api_key = os.getenv("NEWS_API_KEY")
    if not api_key:
        return "News API key not configured."

    _ask_log("Using News API")
    topic = extract_news_topic(question)

    try:
        response = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": topic,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 5,
                "apiKey": api_key,
            },
            timeout=10,
        )
        data = response.json()
        if response.status_code != 200:
            print("[News Error]", data.get("message", "News API request failed."))
            return "Unable to fetch news right now."

        articles = []
        for article in data.get("articles", []):
            title = (article.get("title") or "").strip()
            if not title or title.lower() == "[removed]":
                continue
            source = (article.get("source") or {}).get("name") or "Unknown source"
            description = (
                article.get("description")
                or "No description available."
            ).strip()
            articles.append((title, source, description))
            if len(articles) == 5:
                break

        if not articles:
            return "No recent news found."

        lines = [f"Latest {topic} News:", ""]
        for index, (title, source, description) in enumerate(articles, start=1):
            lines.extend(
                [
                    f"{index}. {title}",
                    f"Source: {source}",
                    f"Description: {description}",
                    "",
                ]
            )
        return "\n".join(lines).strip()
    except Exception as e:
        print("[News Error]", str(e), flush=True)
        return "Unable to fetch news right now."


# -----------------------------------
# Transcript Handler
# -----------------------------------

def handle_transcript_question(
    question: str,
    transcript_text: str,
) -> str:
    if not transcript_text or not transcript_text.strip():
        return "No uploaded transcript available."

    _ask_log("Using transcript semantic search")

    prompt = (
        "You are analyzing an uploaded audio transcript.\n\n"
        "Rules:\n"
        "- Answer ONLY using transcript content.\n"
        "- Do NOT invent information.\n"
        "- Do NOT use outside knowledge.\n"
        "- If the answer is not present in the transcript, say exactly:\n"
        "This topic was not discussed in uploaded audio.\n"
        "- Mention speaker names or labels if available.\n"
        "- Keep the answer concise.\n\n"
        "Transcript:\n"
        f"{transcript_text.strip()}\n\n"
        "Question:\n"
        f"{question}\n\n"
        "Answer:"
    )

    try:
        answer = _generate_gemini_text(prompt)
        if not answer:
            return "Unable to search transcript right now."
        return answer
    except Exception as e:
        print("[Transcript Search Error]", str(e), flush=True)
        return "Unable to search transcript right now."


# -----------------------------------
# General Gemini Handler
# -----------------------------------

def handle_general_question(
    question: str,
) -> str:
    _ask_log("Using Gemini Q&A")

    prompt = (
        "Answer the user's question clearly and directly. "
        "Keep the answer short to medium length, easy to understand, and useful. "
        "Do not use markdown. Do not add unnecessary formatting. "
        "Give only the answer.\n\n"
        f"Question: {question}"
    )

    try:
        answer = _generate_gemini_text(prompt)
        if not answer:
            return "Unable to answer right now."
        return answer
    except Exception as e:
        print("[Gemini Q&A Error]", str(e), flush=True)
        if (
            "exhausted" in str(e).lower()
            or "rate-limited" in str(e).lower()
            or "quota" in str(e).lower()
        ):
            return (
                "Unable to answer right now. "
                "All Gemini API keys are exhausted or rate-limited."
            )
        return "Unable to answer right now."


# -----------------------------------
# Main Router
# -----------------------------------

def answer_question(
    question: str,
    transcript_text: str = "",
) -> dict:
    """
    Main Ask Anything router.
    """

    question_type = (
        detect_question_type(
            question
        )
    )

    _ask_log(f"Detected type: {question_type}")

    # -----------------------
    # Weather
    # -----------------------

    if (
        question_type
        == "weather"
    ):
        answer = (
            handle_weather_question(
                question
            )
        )

        source = WEATHER_SOURCE_LABEL

    # -----------------------
    # News
    # -----------------------

    elif (
        question_type
        == "news"
    ):
        answer = (
            handle_news_question(
                question
            )
        )

        source = "news"

    # -----------------------
    # Transcript
    # -----------------------

    elif (
        question_type
        == "transcript"
    ):
        answer = (
            handle_transcript_question(
                question,
                transcript_text,
            )
        )

        source = "transcript"

    # -----------------------
    # General
    # -----------------------

    else:
        answer = (
            handle_general_question(
                question
            )
        )

        source = "gemini"

    return {
        "question": question,
        "answer": answer,
        "source": source,
    }


# -----------------------------------
# Testing
# -----------------------------------

if __name__ == "__main__":

    transcript = """
    Person 1 - Boxing is a great sport.

    Person 2 - But can boxing make it?

    Person 1 - I love boxing.
    """

    questions = [
        "What is today's weather in Kolhapur?",
        "What is weather in Mumbai?",
        "What is weather in Peth Vadgaon?",
        "Latest AI news",
        "Latest India news",
        "What did speaker 2 say about boxing?",
        "What did person 1 say?",
        "What was discussed about AI?",
        "What is Agentic AI?",
        "What is Machine Learning?",
        "Explain reinforcement learning",
    ]

    for q in questions:

        result = answer_question(
            question=q,
            transcript_text=transcript,
        )

        print("\n")
        print(result)
