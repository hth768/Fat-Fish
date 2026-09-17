package com.feiyu.app.data.settings

import android.content.Context
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.preferencesDataStore
import com.feiyu.app.data.persona.Persona
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "feiyu_settings")

private val KEY_PROVIDER = stringPreferencesKey("provider")
private val KEY_BASE_URL = stringPreferencesKey("base_url")
private val KEY_API_KEY = stringPreferencesKey("api_key")
private val KEY_MODEL = stringPreferencesKey("model")
private val KEY_TEMP = floatPreferencesKey("temperature")

private val KEY_CHAR_NAME = stringPreferencesKey("char_name")
private val KEY_PRESET_ID = stringPreferencesKey("persona_preset")
private val KEY_IDENTITY = stringPreferencesKey("p_identity")
private val KEY_PERSONALITY = stringPreferencesKey("p_personality")
private val KEY_ABILITIES = stringPreferencesKey("p_abilities")
private val KEY_HABITS = stringPreferencesKey("p_habits")
private val KEY_EXTRA = stringPreferencesKey("p_extra")

private val KEY_MEM = booleanPreferencesKey("memory_enabled")
private val KEY_EMB_URL = stringPreferencesKey("emb_url")
private val KEY_EMB_KEY = stringPreferencesKey("emb_key")
private val KEY_EMB_MODEL = stringPreferencesKey("emb_model")
private val KEY_TOPK = intPreferencesKey("topk")
private val KEY_MEM_LIMIT = intPreferencesKey("mem_limit")
private val KEY_DEDUP = floatPreferencesKey("dedup")
private val KEY_WINDOW = intPreferencesKey("history_window")
private val KEY_SUMMARY = booleanPreferencesKey("summary_enabled")
private val KEY_VISION_ON = booleanPreferencesKey("vision_enabled")
private val KEY_VISION_MODEL = stringPreferencesKey("vision_model")

private val KEY_TTS_ON = booleanPreferencesKey("tts_enabled")
private val KEY_TTS_AUTO = booleanPreferencesKey("tts_auto")
private val KEY_TTS_CLOUD = booleanPreferencesKey("tts_use_cloud")
private val KEY_TTS_URL = stringPreferencesKey("tts_url")
private val KEY_TTS_KEY = stringPreferencesKey("tts_key")
private val KEY_TTS_MODEL = stringPreferencesKey("tts_model")
private val KEY_TTS_VOICE = stringPreferencesKey("tts_voice")
private val KEY_TTS_SPEED = floatPreferencesKey("tts_speed")
private val KEY_TTS_PITCH = floatPreferencesKey("tts_pitch")
private val KEY_TTS_PAUSE_S = intPreferencesKey("tts_pause_sentence")
private val KEY_TTS_PAUSE_P = intPreferencesKey("tts_pause_para")
private val KEY_TTS_EMO_KEY = stringPreferencesKey("tts_emo_key")
private val KEY_TTS_EMO_VAL = stringPreferencesKey("tts_emo_val")
private val KEY_TTS_EMO_PRESET = stringPreferencesKey("tts_emo_preset")
private val KEY_TTS_REWRITE = booleanPreferencesKey("tts_rewrite")

class SettingsRepository(private val context: Context) {

    val settingsFlow: Flow<Settings> = context.dataStore.data.map { prefs ->
        Settings(
            providerId = prefs[KEY_PROVIDER] ?: "deepseek",
            baseUrl = prefs[KEY_BASE_URL] ?: "https://api.deepseek.com/v1",
            apiKey = prefs[KEY_API_KEY] ?: "",
            // 迁移：旧版本默认 deepseek-chat，统一升级为 deepseek-flash
            model = (prefs[KEY_MODEL] ?: "deepseek-flash").let {
                if (it == "deepseek-chat") "deepseek-flash" else it
            },
            temperature = prefs[KEY_TEMP] ?: 0.9f,
            characterName = prefs[KEY_CHAR_NAME] ?: Persona.DEFAULT_PRESET.name,
            personaPresetId = prefs[KEY_PRESET_ID] ?: "custom",
            identity = prefs[KEY_IDENTITY] ?: Persona.DEFAULT_PRESET.fields["identity"].orEmpty(),
            personality = prefs[KEY_PERSONALITY] ?: Persona.DEFAULT_PRESET.fields["personality"].orEmpty(),
            abilities = prefs[KEY_ABILITIES] ?: Persona.DEFAULT_PRESET.fields["abilities"].orEmpty(),
            habits = prefs[KEY_HABITS] ?: Persona.DEFAULT_PRESET.fields["habits"].orEmpty(),
            extra = prefs[KEY_EXTRA] ?: Persona.DEFAULT_PRESET.fields["extra"].orEmpty(),
            memoryEnabled = prefs[KEY_MEM] ?: true,
            embeddingBaseUrl = prefs[KEY_EMB_URL] ?: "",
            embeddingApiKey = prefs[KEY_EMB_KEY] ?: "",
            embeddingModel = prefs[KEY_EMB_MODEL] ?: "text-embedding-3-small",
            topK = prefs[KEY_TOPK] ?: 5,
            memoryLimit = prefs[KEY_MEM_LIMIT] ?: 800,
            dedupThreshold = prefs[KEY_DEDUP] ?: 0.95f,
            historyWindow = prefs[KEY_WINDOW] ?: 12,
            summaryEnabled = prefs[KEY_SUMMARY] ?: true,
            visionEnabled = prefs[KEY_VISION_ON] ?: true,
            visionModel = prefs[KEY_VISION_MODEL] ?: "deepseek-flash",
            ttsEnabled = prefs[KEY_TTS_ON] ?: true,
            ttsAutoSpeak = prefs[KEY_TTS_AUTO] ?: false,
            ttsUseCloud = prefs[KEY_TTS_CLOUD] ?: false,
            ttsBaseUrl = prefs[KEY_TTS_URL] ?: "",
            ttsApiKey = prefs[KEY_TTS_KEY] ?: "",
            ttsModel = prefs[KEY_TTS_MODEL] ?: "tts-1",
            ttsVoice = prefs[KEY_TTS_VOICE] ?: "alloy",
            ttsSpeed = prefs[KEY_TTS_SPEED] ?: 1.0f,
            ttsPitch = prefs[KEY_TTS_PITCH] ?: 1.0f,
            ttsSentencePause = prefs[KEY_TTS_PAUSE_S] ?: 120,
            ttsParagraphPause = prefs[KEY_TTS_PAUSE_P] ?: 320,
            ttsEmotionKey = prefs[KEY_TTS_EMO_KEY] ?: "",
            ttsEmotionValue = prefs[KEY_TTS_EMO_VAL] ?: "",
            ttsEmotionPresetId = prefs[KEY_TTS_EMO_PRESET] ?: "custom",
            ttsRewriteEnabled = prefs[KEY_TTS_REWRITE] ?: false,
        )
    }

    suspend fun update(block: (Settings) -> Settings) {
        val current = settingsFlow.first()
        val next = block(current)
        context.dataStore.edit { prefs ->
            prefs[KEY_PROVIDER] = next.providerId
            prefs[KEY_BASE_URL] = next.baseUrl
            prefs[KEY_API_KEY] = next.apiKey
            prefs[KEY_MODEL] = next.model
            prefs[KEY_TEMP] = next.temperature
            prefs[KEY_CHAR_NAME] = next.characterName
            prefs[KEY_PRESET_ID] = next.personaPresetId
            prefs[KEY_IDENTITY] = next.identity
            prefs[KEY_PERSONALITY] = next.personality
            prefs[KEY_ABILITIES] = next.abilities
            prefs[KEY_HABITS] = next.habits
            prefs[KEY_EXTRA] = next.extra
            prefs[KEY_MEM] = next.memoryEnabled
            prefs[KEY_EMB_URL] = next.embeddingBaseUrl
            prefs[KEY_EMB_KEY] = next.embeddingApiKey
            prefs[KEY_EMB_MODEL] = next.embeddingModel
            prefs[KEY_TOPK] = next.topK
            prefs[KEY_MEM_LIMIT] = next.memoryLimit
            prefs[KEY_DEDUP] = next.dedupThreshold
            prefs[KEY_WINDOW] = next.historyWindow
            prefs[KEY_SUMMARY] = next.summaryEnabled
            prefs[KEY_VISION_ON] = next.visionEnabled
            prefs[KEY_VISION_MODEL] = next.visionModel
            prefs[KEY_TTS_ON] = next.ttsEnabled
            prefs[KEY_TTS_AUTO] = next.ttsAutoSpeak
            prefs[KEY_TTS_CLOUD] = next.ttsUseCloud
            prefs[KEY_TTS_URL] = next.ttsBaseUrl
            prefs[KEY_TTS_KEY] = next.ttsApiKey
            prefs[KEY_TTS_MODEL] = next.ttsModel
            prefs[KEY_TTS_VOICE] = next.ttsVoice
            prefs[KEY_TTS_SPEED] = next.ttsSpeed
            prefs[KEY_TTS_PITCH] = next.ttsPitch
            prefs[KEY_TTS_PAUSE_S] = next.ttsSentencePause
            prefs[KEY_TTS_PAUSE_P] = next.ttsParagraphPause
            prefs[KEY_TTS_EMO_KEY] = next.ttsEmotionKey
            prefs[KEY_TTS_EMO_VAL] = next.ttsEmotionValue
            prefs[KEY_TTS_EMO_PRESET] = next.ttsEmotionPresetId
            prefs[KEY_TTS_REWRITE] = next.ttsRewriteEnabled
        }
    }
}
