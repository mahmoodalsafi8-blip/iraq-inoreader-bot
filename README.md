# ربات اخبار عراق (Inoreader ← DeepSeek ← تلگرام)

هر ۳۰ دقیقه اخبار جدید پوشه `Iraq` در Inoreader را می‌خواند، خبرهای مربوط به عراق را جدا می‌کند، با DeepSeek به فارسی ترجمه و خلاصه می‌کند و در کانال `@Iraqinoreader` منتشر می‌کند.

## فایل‌ها

| فایل | کار |
|---|---|
| `bot.py` | کد اصلی ربات |
| `keywords.txt` | کلیدواژه‌های فیلتر اولیه (خودتان می‌توانید کم و زیاد کنید) |
| `.github/workflows/bot.yml` | دستور اجرا در GitHub Actions |

## Secrets لازم

| نام | توضیح |
|---|---|
| `INOREADER_APP_ID` | از صفحه Developer اینوریدر |
| `INOREADER_APP_KEY` | از صفحه Developer اینوریدر |
| `INOREADER_REFRESH_TOKEN` | از ابزار گرفتن توکن |
| `TELEGRAM_BOT_TOKEN` | از BotFather |
| `TELEGRAM_CHANNEL` | `@Iraqinoreader` |
| `DEEPSEEK_API_KEY` | از platform.deepseek.com |
| `GH_PAT` | توکن گیت‌هاب برای ذخیره خودکار توکن تمدیدشده اینوریدر |

## منطق کار

1. خبرهای چند ساعت اخیر پوشه `Iraq` که برچسب `checked-iraq` ندارند خوانده می‌شوند.
2. فیلتر کلیدواژه: خبرهای بی‌ربط همان‌جا کنار می‌روند.
3. حداکثر ۱۵ خبر در هر اجرا به DeepSeek می‌رود (موازی) تا ارتباط با عراق تأیید و ترجمه شود.
4. خبرهای تأییدشده در کانال منتشر می‌شوند.
5. همه خبرهای بررسی‌شده برچسب `checked-iraq` و خبرهای منتشرشده برچسب `sent-iraq` می‌گیرند.

## اجرای آزمایشی

**Actions ← Iraq News Bot ← Run workflow** و تیک **dry_run** را بزنید. پست‌ها فقط در لاگ نمایش داده می‌شوند و چیزی منتشر یا برچسب‌گذاری نمی‌شود.
