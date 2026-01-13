import os
import shutil
import speech_recognition as sr
from gtts import gTTS
from pydub import AudioSegment
import uuid

def transcribe_with_auto_language(audio_path):
    """
    Transcribes audio file to text.
    Returns: (text, language_code, error_message)
    """
    recognizer = sr.Recognizer()
    
    # Convert to WAV if needed (pydub handles this)
    # SpeechRecognition prefers WAV
    wav_path = audio_path
    converted = False
    
    if not audio_path.lower().endswith(".wav"):
        try:
             # Convert to wav
             sound = AudioSegment.from_file(audio_path)
             wav_path = f"{audio_path}.wav"
             sound.export(wav_path, format="wav")
             converted = True
        except Exception as e:
            return None, None, f"Audio conversion failed: {str(e)}"

    try:
        with sr.AudioFile(wav_path) as source:
            # record the audio data
            audio_data = recognizer.record(source)
            # recognize speech using Google Speech Recognition
            # We can try to detect language, but standard google requires code.
            # We'll default to 'en-US' or try to iterate if needed, but for now fixed to en-US or universal if possible.
            # actually recognize_google doesn't auto-detect well without a hint.
            # We will try generic English first.
            text = recognizer.recognize_google(audio_data)
            return text, "en", None
    except sr.UnknownValueError:
        return None, None, "Could not understand audio"
    except sr.RequestError as e:
        return None, None, f"Speech service error: {e}"
    except Exception as e:
        return None, None, f"Transcription error: {str(e)}"
    finally:
        if converted and os.path.exists(wav_path):
            os.remove(wav_path)

def detect_language_from_text(text):
    """
    Simple heuristic or placeholder for language detection.
    Real implementation would use `langdetect` or `polyglot`.
    For now, we default to 'en'.
    """
    # TODO: Add real language detection if needed.
    # Simple check for Hindi characters?
    for char in text:
        if '\u0900' <= char <= '\u097f':
            return 'hi'
    return 'en'

def text_to_speech(text, lang='en'):
    """
    Converts text to speech using gTTS.
    Returns: (audio_file_path, error_message)
    """
    try:
        # Create a unique filename
        filename = f"response_{uuid.uuid4()}.mp3"
        # Use a temp dir or local dir
        save_path = os.path.join(os.path.dirname(__file__), "..", "data", "audio_cache")
        if not os.path.exists(save_path):
            os.makedirs(save_path, exist_ok=True)
            
        full_path = os.path.join(save_path, filename)
        
        tts = gTTS(text=text, lang=lang, slow=False)
        tts.save(full_path)
        
        return full_path, None
    except Exception as e:
        return None, f"TTS Error: {str(e)}"

def cleanup_temp_files(path):
    """Removes the file if it exists."""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except:
            pass
