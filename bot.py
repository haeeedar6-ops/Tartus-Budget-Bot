import telebot
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import json
import os
from datetime import datetime
import calendar
from flask import Flask
from threading import Thread

# 1. إعداد المفاتيح
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# 2. إعداد قاعدة بيانات جوجل (Firebase)
cred = credentials.Certificate("firebase_credentials.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# 3. إعداد نماذج الذكاء الاصطناعي (Gemini) 
expense_instruction = """
أنت محاسب ذكي في سوريا (طرطوس). مهمتك استخراج التكلفة الإجمالية المدفوعة من رسالة المستخدم.
يجب أن ترد بصيغة JSON حصراً بهذا الشكل:
{"total_spent": 15000, "message": "تم تسجيل المصروف، السعر يعتبر طبيعي أو مرتفع قليلاً..."}
"""
# إجبار النموذج على إرجاع JSON سليم
expense_model = genai.GenerativeModel(
    model_name="gemini-1.5-pro",
    system_instruction=expense_instruction,
    generation_config={"response_mime_type": "application/json"} 
)

planning_instruction = """
أنت مستشار مالي وطاهٍ اقتصادي في سوريا (طرطوس). 
مهمتك قراءة الميزانية اليومية المتاحة للمستخدم، واقتراح خطة مشتريات أو طبخة تناسب تماماً هذا المبلغ بناءً على أسعار السوق التقريبية في طرطوس.
رد بنص واضح ومنسق، بدون JSON.
"""
planning_model = genai.GenerativeModel(
    model_name="gemini-1.5-pro",
    system_instruction=planning_instruction
)

# 4. دوال إدارة الميزانية
def check_and_rollover_budget(user_id):
    user_id_str = str(user_id)
    current_month = datetime.now().strftime("%Y-%m")
    doc_ref = db.collection('budgets').document(user_id_str)
    doc = doc_ref.get()
    
    if doc.exists:
        data = doc.to_dict()
        base_budget = data.get('base_budget', 2000000)
        current_budget = data.get('current_budget', 2000000)
        saved_month = data.get('current_month', current_month)
        
        if saved_month != current_month:
            new_budget = base_budget + current_budget
            doc_ref.update({'current_budget': new_budget, 'current_month': current_month})
            bot.send_message(user_id, f"📅 شهر جديد! تم ترحيل الرصيد. ميزانيتك: {new_budget:,.0f} ل.س")
    else:
        doc_ref.set({'base_budget': 2000000, 'current_budget': 2000000, 'current_month': current_month})

# 5. أوامر التلغرام
@bot.message_handler(commands=['start'])
def start_message(message):
    check_and_rollover_budget(message.chat.id)
    bot.reply_to(message, "مرحباً بك! أنا مساعدك المالي.\n- أرسل أي مصروف لتسجيله.\n- استخدم /registration لتحديد ميزانيتك.\n- استخدم /today لمعرفة ميزانية اليوم واقتراحات الطبخ.")

@bot.message_handler(commands=['registration'])
def registration_message(message):
    try:
        amount = float(message.text.split()[1])
        user_id_str = str(message.chat.id)
        current_month = datetime.now().strftime("%Y-%m")
        db.collection('budgets').document(user_id_str).set({
            'base_budget': amount,
            'current_budget': amount,
            'current_month': current_month
        }, merge=True)
        bot.reply_to(message, f"✅ تم حفظ ميزانيتك الشهرية بنجاح: {amount:,.0f} ل.س.")
    except Exception as e:
        print(f"Registration Error: {e}")
        bot.reply_to(message, "⚠️ اكتب الأمر وبجانبه الرقم. مثال: `/registration 2000000`", parse_mode="Markdown")

@bot.message_handler(commands=['today', 'fortheday'])
def today_plan_message(message):
    check_and_rollover_budget(message.chat.id)
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        user_id_str = str(message.chat.id)
        doc = db.collection('budgets').document(user_id_str).get()
        rem_budget = doc.to_dict().get('current_budget', 0) if doc.exists else 0
        
        today = datetime.now()
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        days_left = days_in_month - today.day + 1
        daily_allowance = rem_budget / days_left if days_left > 0 else rem_budget
        
        today_str = today.strftime("%Y-%m-%d")
        # تم إصلاح تحذير الفلترة هنا
        expenses_ref = db.collection('expenses').where(filter=FieldFilter('user_id', '==', message.chat.id)).where(filter=FieldFilter('date', '==', today_str)).stream()
        spent_today = sum(emp.to_dict().get('amount', 0) for emp in expenses_ref)
        
        remaining_for_today = daily_allowance - spent_today
        prompt = f"التاريخ: {today_str}. الميزانية اليومية المسموحة للمستخدم هي {daily_allowance} ل.س. لقد صرف منها اليوم {spent_today} ل.س. المتبقي له لليوم هو {remaining_for_today} ل.س. اقترح عليه خطة مشتريات أو طبخة تناسب المبلغ المتبقي."
        
        response = planning_model.generate_content(prompt)
        plan_text = f"📊 **إحصائياتك لليوم:**\n- المسموح يومياً: {daily_allowance:,.0f} ل.س\n- صرفت اليوم: {spent_today:,.0f} ل.س\n- المتبقي لليوم: {remaining_for_today:,.0f} ل.س\n\n💡 **اقتراح اليوم:**\n{response.text}"
        bot.reply_to(message, plan_text, parse_mode="Markdown")
    except Exception as e:
        print(f"Today Plan Error: {e}") # لطباعة الخطأ في Render
        bot.reply_to(message, "حدث خطأ أثناء جلب خطة اليوم.")

@bot.message_handler(func=lambda message: True)
def handle_expenses(message):
    check_and_rollover_budget(message.chat.id)
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        prompt = f"التاريخ اليوم: {datetime.now().strftime('%Y-%m-%d')}.\nرسالة المستخدم: {message.text}"
        response = expense_model.generate_content(prompt)
        data = json.loads(response.text)
        
        amount = float(data.get("total_spent", 0))
        msg = data.get("message", "تم التسجيل.")
        
        if amount > 0:
            db.collection('expenses').add({
                'user_id': message.chat.id, 'amount': amount, 'description': message.text, 
                'date': datetime.now().strftime("%Y-%m-%d"), 'month': datetime.now().strftime("%Y-%m")
            })
            user_ref = db.collection('budgets').document(str(message.chat.id))
            user_ref.update({'current_budget': firestore.Increment(-amount)})
            
            rem = user_ref.get().to_dict().get('current_budget', 0)
            bot.reply_to(message, f"{msg}\n💰 المتبقي للشهر: {rem:,.0f} ل.س")
        else:
            bot.reply_to(message, msg)
    except Exception as e:
        print(f"Expense Handling Error: {e}") # لطباعة الخطأ في Render
        bot.reply_to(message, "حدث خطأ. تأكد من كتابة المصروف بوضوح.")

# 6. سيرفر Flask
app = Flask(__name__)
@app.route('/')
def home():
    return "البوت يعمل بنجاح ومربوط بـ Firebase!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    Thread(target=run_web).start()
    bot.infinity_polling()
