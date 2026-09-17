package com.feiyu.app.data.image

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.util.UUID
import kotlin.math.max

/**
 * 图片处理工具。
 *
 * 上传前会做压缩：多数视觉模型对图片有尺寸/体积限制，
 * 直接传原图（手机拍摄常 3~8MB）容易超限或极慢。
 */
object ImageUtils {

    /** 压缩目标：最长边像素上限 */
    private const val MAX_DIMEN = 1280

    /** JPEG 质量（0~100） */
    private const val JPEG_QUALITY = 82

    /**
     * 从相册 Uri 读取图片 → 压缩 → 存入应用私有目录，返回本地文件。
     * 返回的文件同时用于「界面显示」与「Base64 上传」。
     */
    suspend fun importToCache(context: Context, uri: Uri): File? = withContext(Dispatchers.IO) {
        try {
            val original = context.contentResolver.openInputStream(uri)?.use { input ->
                BitmapFactory.decodeStream(input)
            } ?: return@withContext null

            val scaled = scaleDown(original)
            val byteArray = ByteArrayOutputStream().use { out ->
                scaled.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
                out.toByteArray()
            }
            if (scaled !== original) scaled.recycle()
            original.recycle()

            val dir = File(context.filesDir, "images").apply { mkdirs() }
            val file = File(dir, "img_${UUID.randomUUID()}.jpg")
            FileOutputStream(file).use { it.write(byteArray) }
            file
        } catch (_: Exception) {
            null
        }
    }

    /** 等比缩放到最长边不超过 MAX_DIMEN。 */
    private fun scaleDown(src: Bitmap): Bitmap {
        val longest = max(src.width, src.height)
        if (longest <= MAX_DIMEN) return src
        val ratio = MAX_DIMEN.toFloat() / longest
        val w = (src.width * ratio).toInt().coerceAtLeast(1)
        val h = (src.height * ratio).toInt().coerceAtLeast(1)
        return Bitmap.createScaledBitmap(src, w, h, true)
    }

    /**
     * 把本地图片文件编码成 data URL（`data:image/jpeg;base64,...`）。
     * OpenAI 兼容接口的 image_url 字段接受 data URL 形式。
     */
    suspend fun toDataUrl(file: File): String? = withContext(Dispatchers.IO) {
        try {
            if (!file.exists()) return@withContext null
            val bytes = file.readBytes()
            val b64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
            "data:image/jpeg;base64,$b64"
        } catch (_: Exception) {
            null
        }
    }

    /** 删除图片文件（清理用）。 */
    fun delete(file: File) {
        try {
            if (file.exists()) file.delete()
        } catch (_: Exception) {
            // ignore
        }
    }
}
