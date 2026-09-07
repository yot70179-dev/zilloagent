# הפעלת שליחה אוטומטית מהמספר שלך (WhatsApp Cloud API)

מדריך צעד-אחר-צעד להפעלת המצב האוטומטי — הסוכן שולח **מהמספר שלך** בצורה חוקית,
הודעה אחת בשעה, 10 לישראל + 5 לארה"ב ביום.

> ⚠️ המספר שתחבר עובר ל-WhatsApp Business Platform (Cloud API) ולא ישמש יותר
> באפליקציית WhatsApp הרגילה על אותו מכשיר. שווה לחבר מספר ייעודי לעסק.

## שלב 1 — חשבון Meta ואפליקציה
1. היכנס ל-<https://developers.facebook.com> והתחבר.
2. **My Apps → Create App → Business → WhatsApp**.
3. תחת **WhatsApp → API Setup** תראה `Phone number ID` ו-`Temporary access token`.

## שלב 2 — חיבור המספר שלך
1. ב-**API Setup → Step 1**, לחץ **Add phone number** והזן את המספר שלך.
2. אמת בקוד SMS/שיחה.
3. העתק את **Phone number ID** → זה `WHATSAPP_PHONE_ID`.

## שלב 3 — טוקן קבוע (permanent token)
הטוקן הזמני פג אחרי 24 שעות. לטוקן קבוע:
1. **Business Settings → Users → System Users → Add** (משתמש מערכת, Admin).
2. **Add Assets → Apps →** האפליקציה שלך → הרשאת Manage.
3. **Generate new token →** בחר את האפליקציה, סמן `whatsapp_business_messaging`
   ו-`whatsapp_business_management` → העתק → זה `WHATSAPP_TOKEN`.

## שלב 4 — תבנית הודעה מאושרת (חובה לפנייה קרה!)
Cloud API **חוסם טקסט חופשי** למי שלא כתב לך ב-24 השעות האחרונות, לכן ההודעה
הראשונה חייבת להיות **Template** מאושרת:
1. **WhatsApp Manager → Message Templates → Create template**.
2. Category: **Marketing**. Name: למשל `landing_page_intro`.
3. Language: עברית (וגם English אם רוצים ארה"ב).
4. גוף ההודעה — הדבק בדיוק (עם שני משתנים `{{1}}` = שם, `{{2}}` = שם העסק):
   ```
   היי {{1}}, שמי יותם, מעצב דפי נחיתה. בניתי דוגמה של דף נחיתה ל{{2}} רק כדי להראות איך זה יכול להיראות — חשוב לי שתדע/י שזו דוגמה ראשונית בלבד. אם תרצה/י אשמח לשלוח לך אותה, ונוכל להתקדם למשהו מותאם ואמיתי. תודה על זמנך!
   ```
   > הטקסט ב-`whatsapp_sender.py` (`TEMPLATE_TEXT`) חייב להיות זהה לתבנית שאישרת.
5. שלח לאישור (בדרך כלל דקות עד שעות). כששמו מאושר → `WHATSAPP_TEMPLATE_NAME=landing_page_intro`.

## שלב 5 — Webhook לתגובות (כדי שתקבל מייל מיד כשמישהו עונה)
1. ב-**WhatsApp → Configuration → Webhook → Edit**.
2. Callback URL: `https://<כתובת-השרת-שלך>/webhooks/whatsapp`
3. Verify token: בחר מחרוזת כלשהי והכנס אותה גם ב-`WHATSAPP_VERIFY_TOKEN`.
4. **Verify and save**, ואז **Subscribe** לשדה `messages`.

## שלב 6 — משתני סביבה (Render/Railway)
```
WHATSAPP_TOKEN=EAAG...            # שלב 3
WHATSAPP_PHONE_ID=1234567890      # שלב 2
WHATSAPP_VERIFY_TOKEN=my-secret   # שלב 5
WHATSAPP_TEMPLATE_NAME=landing_page_intro   # שלב 4
MK_SENDER_NAME=יותם
MK_REPORT_EMAIL=you@example.com   # לכאן מגיעות תגובות + דוח חודשי
GOOGLE_PLACES_API_KEY=...         # למשיכת מספרים אוטומטית (אופציונלי)
MK_SOURCE_QUERIES=IL|מספרות בתל אביב;US|plumbers in Austin TX
```

## מה קורה אוטומטית אחרי זה
- **כל בוקר (09:30 שעון ישראל):** מושך מספרים חדשים מ-Google Places, ובונה תור של 10 ישראל + 5 ארה"ב.
- **כל שעה:** שולח הודעה אחת מהמספר שלך (בשעות הפעילות, 09:00–21:00 מקומי).
- **כשמישהו עונה:** מייל אליך מיד. מי שכותב "הסר"/"STOP" — לא יקבל שוב לעולם.
- **ב-1 בכל חודש:** מייל עם סיכום כל הפעילות.

הכול נשלט דרך `MK_ENABLED`, `MK_IL_DAILY_CAP`, `MK_US_DAILY_CAP` ב-`.env`.
