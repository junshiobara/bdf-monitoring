"""
BdF刊行物監視システム（Gmail API版）
LesEchos Botと同じ方式でGmail API使用
"""

import asyncio
import os
import sys
import base64
import time
import json
import schedule
import requests
from pathlib import Path
from email.mime.text import MimeText
from email.mime.multipart import MimeMultipart
from datetime import datetime, timedelta
import pytz
import logging
from bs4 import BeautifulSoup
import re
from typing import Dict, List, Optional

import google.auth.transport.requests
import google.oauth2.credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

# ログ設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─── 環境変数読み込み ─────────────────────────────
load_dotenv("credentials/.env", encoding="utf-8-sig")
GMAIL_TO = os.getenv("GMAIL_TO")

# 必須チェック
if not GMAIL_TO:
    sys.exit("❌ credentials/.env に GMAIL_TO がありません")

class BdFGmailAPINotifier:
    def __init__(self):
        self.base_url = "https://www.banque-france.fr"
        self.publications_url = f"{self.base_url}/en/publications-and-statistics/publications"
        self.paris_tz = pytz.timezone('Europe/Paris')
        self.data_file = 'bdf_gmail_api_data.json'
        self.gmail_to = GMAIL_TO
        
        # Gmail API初期化
        self.gmail_service = self.init_gmail()
        
        # セッション設定
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        # 定期刊行物パターン
        self.publication_patterns = {
            'monthly_business_survey': {
                'title': 'Monthly Business Survey',
                'pattern': r'monthly business survey',
                'priority': 'High'
            },
            'macroeconomic_projections': {
                'title': 'Macroeconomic Projections',
                'pattern': r'macroeconomic projections',
                'priority': 'High'
            },
            'financial_stability_report': {
                'title': 'Financial Stability Report',
                'pattern': r'financial stability report',
                'priority': 'Highest'
            },
            'letter_to_president': {
                'title': 'Letter to the President of the Republic',
                'pattern': r'letter to the president',
                'priority': 'Medium'
            },
            'balance_of_payments': {
                'title': 'Balance of Payments Report',
                'pattern': r'balance of payments.*international investment',
                'priority': 'Medium'
            },
            'observatory_payment_security': {
                'title': 'Observatory Security Payment Report',
                'pattern': r'observatory.*security.*payment',
                'priority': 'Medium'
            },
            'annual_report': {
                'title': 'Banque de France Annual Report',
                'pattern': r'banque de france annual report',
                'priority': 'Medium'
            }
        }
        
        self.load_data()

    def init_gmail(self):
        """Gmail API初期化（LesEchos方式）"""
        SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
        CRED = Path("credentials")
        TOKEN = CRED / "token.json"
        SECRET = CRED / "credentials.json"
        
        try:
            if not TOKEN.exists():
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow = InstalledAppFlow.from_client_secrets_file(SECRET, SCOPES)
                creds = flow.run_local_server(port=0)
                TOKEN.write_text(creds.to_json())
            
            creds = google.oauth2.credentials.Credentials.from_authorized_user_file(TOKEN, SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(google.auth.transport.requests.Request())
                TOKEN.write_text(creds.to_json())
            
            gmail_service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            logger.info("✅ Gmail API 初期化完了")
            return gmail_service
            
        except Exception as e:
            logger.error(f"❌ Gmail API 初期化エラー: {e}")
            logger.error("credentials/credentials.json が正しく配置されているか確認してください")
            sys.exit(1)

    def load_data(self):
        """過去の刊行物データ読み込み"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    self.known_publications = json.load(f)
            else:
                self.known_publications = {}
            logger.info(f"データ読み込み完了: {len(self.known_publications)} 件の既知刊行物")
        except Exception as e:
            logger.error(f"データ読み込みエラー: {e}")
            self.known_publications = {}

    def save_data(self):
        """データ保存"""
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.known_publications, f, ensure_ascii=False, indent=2)
            logger.info("データ保存完了")
        except Exception as e:
            logger.error(f"データ保存エラー: {e}")

    def extract_publications_from_page(self):
        """BdFページから刊行物を抽出"""
        try:
            response = self.session.get(self.publications_url, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            publications = []
            
            # HTMLから刊行物を検索
            # 方法1: タイトルを含む要素を検索
            for element in soup.find_all(['h1', 'h2', 'h3', 'h4', 'a']):
                text = element.get_text(strip=True)
                if text and len(text) > 15:
                    # 各パターンでチェック
                    for pub_id, pub_config in self.publication_patterns.items():
                        if re.search(pub_config['pattern'], text, re.IGNORECASE):
                            # 日付を抽出
                            date_text = self.extract_date_near_element(element)
                            
                            pub_data = {
                                'pub_id': pub_id,
                                'title': text,
                                'date_text': date_text,
                                'pattern_matched': pub_config['pattern'],
                                'priority': pub_config['priority'],
                                'extracted_at': datetime.now(self.paris_tz).isoformat(),
                                'source_tag': element.name
                            }
                            publications.append(pub_data)
                            break
            
            # 方法2: ページ全体のテキストからも検索
            page_text = soup.get_text()
            for pub_id, pub_config in self.publication_patterns.items():
                matches = list(re.finditer(pub_config['pattern'], page_text, re.IGNORECASE))
                
                for match in matches:
                    # 既に見つかったタイトルと重複チェック
                    context_start = max(0, match.start() - 200)
                    context_end = min(len(page_text), match.end() + 200)
                    context = page_text[context_start:context_end]
                    
                    # コンテキストから完全なタイトルを抽出
                    title = self.extract_title_from_context(context)
                    date_text = self.extract_date_from_context(context)
                    
                    if title and len(title) > 15:
                        # 重複チェック
                        is_duplicate = any(
                            pub['title'].lower() == title.lower() 
                            for pub in publications
                        )
                        
                        if not is_duplicate:
                            pub_data = {
                                'pub_id': pub_id,
                                'title': title,
                                'date_text': date_text,
                                'pattern_matched': pub_config['pattern'],
                                'priority': pub_config['priority'],
                                'extracted_at': datetime.now(self.paris_tz).isoformat(),
                                'source_tag': 'text_search'
                            }
                            publications.append(pub_data)
            
            logger.info(f"ページから {len(publications)} 件の刊行物を抽出")
            return publications
            
        except Exception as e:
            logger.error(f"ページ抽出エラー: {e}")
            return []

    def extract_date_near_element(self, element):
        """要素の近くから日付を抽出"""
        # 同じ親要素内から日付を検索
        parent = element.parent
        if parent:
            text = parent.get_text()
            return self.extract_date_from_context(text)
        return ''

    def extract_title_from_context(self, context):
        """コンテキストから完全なタイトルを抽出"""
        lines = context.split('\n')
        for line in lines:
            cleaned = line.strip()
            if len(cleaned) > 20 and len(cleaned) < 200:
                # 無効なパターンを除外
                if not re.search(r'^(Home|Menu|Search|Filter)', cleaned, re.IGNORECASE):
                    return cleaned
        return ''

    def extract_date_from_context(self, context):
        """コンテキストから日付を抽出"""
        date_patterns = [
            r'(\d{1,2}(?:st|nd|rd|th)?\s+of\s+\w+\s+\d{4})',
            r'(\w+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})',
            r'(\d{1,2}[/-]\d{1,2}[/-]\d{4})'
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, context, re.IGNORECASE)
            if match:
                return match.group(1)
        return ''

    def check_for_new_publications(self):
        """新しい刊行物をチェック"""
        logger.info("🔍 新しい刊行物をチェック中...")
        
        

    def send_gmail_notification(self, matching_publications):
        """Gmail API経由で刊行物通知を送信（LesEchos方式）"""
        try:
            subject = f"🏦 BdF刊行物通知 - {len(matching_publications)}件の対象刊行物"
            
            # HTML形式のメール本文作成
            html_body = self.create_notification_html(matching_publications)
            
            # MIMEメッセージ作成
            msg = MimeMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.gmail_to
            msg['To'] = self.gmail_to
            
            # HTML版を添付
            html_part = MimeText(html_body, 'html', 'utf-8')
            msg.attach(html_part)
            
            # Base64エンコード
            raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            
            # Gmail API経由で送信
            self.gmail_service.users().messages().send(
                userId="me", 
                body={"raw": raw_message}
            ).execute()
            
            logger.info(f"✅ Gmail送信完了: {self.gmail_to}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Gmail送信エラー: {e}")
            return False

    def create_notification_html(self, matching_publications):
        """HTML形式の通知メール作成"""
        html = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .header {{ 
                    background-color: #2563eb; 
                    color: white; 
                    padding: 20px; 
                    text-align: center; 
                    border-radius: 8px 8px 0 0;
                }}
                .content {{ padding: 20px; }}
                .publication {{ 
                    border-left: 4px solid #2563eb; 
                    padding: 15px; 
                    margin: 15px 0; 
                    background-color: #f8f9fa; 
                    border-radius: 0 8px 8px 0;
                }}
                .title {{ 
                    font-weight: bold; 
                    font-size: 1.1em; 
                    color: #1f2937; 
                    margin-bottom: 8px;
                }}
                .details {{ 
                    color: #6b7280; 
                    font-size: 0.9em; 
                    line-height: 1.4;
                }}
                .priority-highest {{ border-left-color: #dc2626; }}
                .priority-high {{ border-left-color: #f59e0b; }}
                .priority-medium {{ border-left-color: #10b981; }}
                .footer {{ 
                    background-color: #f3f4f6; 
                    padding: 15px; 
                    margin-top: 20px; 
                    color: #6b7280; 
                    font-size: 0.8em; 
                    border-radius: 8px;
                }}
                .url-link {{
                    color: #2563eb;
                    text-decoration: none;
                    word-break: break-all;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>🏦 Banque de France</h1>
                <h2>定期刊行物が公表されました</h2>
            </div>
            
            <div class="content">
                <p><strong>{len(matching_publications)}件</strong>の対象刊行物が発見されました:</p>
        """
        
        for i, pub in enumerate(matching_publications, 1):
            priority_class = f"priority-{pub['priority'].lower()}"
            
            html += f"""
                <div class="publication {priority_class}">
                    <div class="title">{i}. {pub['title']}</div>
                    <div class="details">
                        📅 <strong>発行日:</strong> {pub['date_text'] or '日付不明'}<br>
                        🔖 <strong>カテゴリ:</strong> {self.publication_patterns[pub['pub_id']]['title']}<br>
                        ⭐ <strong>優先度:</strong> {pub['priority']}<br>
                        🕐 <strong>検出日時:</strong> {pub['extracted_at'][:19]}<br>
                        🔍 <strong>抽出方法:</strong> {pub['source_tag']}<br>
                        🔗 <strong>データソース:</strong> <a href="{self.publications_url}" class="url-link">{self.publications_url}</a>
                    </div>
                </div>
            """
        
        html += f"""
            </div>
            
            <div class="footer">
                <p><strong>🤖 BdF刊行物監視システム</strong></p>
                <p>📧 送信日時: {datetime.now(self.paris_tz).strftime('%Y-%m-%d %H:%M:%S')} (パリ時間)</p>
                <p>📊 監視対象: {len(self.publication_patterns)} 種類の定期刊行物</p>
                <p>🔄 このメールは自動送信されました。</p>
            </div>
        </body>
        </html>
        """
        
        return html

    def run_daily_check(self):
        """日次チェック実行"""
        paris_now = datetime.now(self.paris_tz)
        logger.info(f"🔍 日次チェック開始: {paris_now.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 8シリーズの刊行物をチェック
        matching_publications = self.check_for_new_publications()
        
        if matching_publications:
            # 対象の刊行物が見つかったらメール送信
            success = self.send_gmail_notification(matching_publications)
            if success:
                logger.info("✅ 刊行物通知送信完了")
            else:
                logger.error("❌ 通知送信失敗")
        else:
            logger.info("ℹ️ 対象の刊行物なし - 通知送信なし")

    def start_scheduler(self):
        """スケジューラー開始"""
        # 毎日パリ時間7:00に実行
        schedule.every().day.at("07:00").do(self.run_daily_check)
        
        logger.info("📅 新刊行物チェックスケジューラー開始: 毎日パリ時間7:00に実行")
        
        def run_scheduler():
            while True:
                schedule.run_pending()
                time.sleep(60)
        
        import threading
        scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
        scheduler_thread.start()

    def test_notification(self):
        """テスト通知送信"""
        test_publications = [{
            'pub_id': 'test',
            'title': 'テスト通知 - BdF刊行物監視システム',
            'date_text': '2025年6月23日',
            'priority': 'Test',
            'pattern_matched': 'test',
            'extracted_at': datetime.now(self.paris_tz).isoformat(),
            'source_tag': 'manual_test'
        }]
        
        logger.info("🧪 テスト通知を送信します...")
        success = self.send_gmail_notification(test_publications)
        return success


def main():
    """メイン実行"""
    print("🚀 BdF刊行物監視システム（Gmail API版）開始")
    print("=" * 60)
    print(f"📧 通知先: {GMAIL_TO}")
    print("📁 設定ファイル: credentials/.env")
    print("🔑 認証: credentials/credentials.json")
    
    # システム初期化
    notifier = BdFGmailAPINotifier()
    
    # テスト通知
    test = input("\nテスト通知を送信しますか？ (y/N): ").lower() == 'y'
    if test:
        notifier.test_notification()
    
    # 手動チェック
    manual = input("手動チェックを実行しますか？ (y/N): ").lower() == 'y'
    if manual:
        notifier.run_daily_check()
    
    # スケジューラー開始
    notifier.start_scheduler()
    
    print(f"\n✅ システム稼働中")
    print(f"⏰ 自動チェック: 毎日パリ時間7:00")
    print(f"📊 監視パターン: {len(notifier.publication_patterns)} 種類")
    print("\n手動チェック: 'm' + Enter")
    print("終了: Ctrl+C")
    
    try:
        while True:
            user_input = input()
            if user_input.lower() == 'm':
                notifier.run_daily_check()
    except KeyboardInterrupt:
        print("\n⏹️ システム停止")


if __name__ == "__main__":
    main()