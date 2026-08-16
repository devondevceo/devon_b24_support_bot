"""Дизайн-система приложения в Битрикс24.

Зачем отдельный модуль: раньше разметка собиралась f-строками прямо в обработчиках,
и каждый новый экран добавлял ещё немного вёрстки в бизнес-логику. Ни шкалы
размеров, ни палитры, ни повторно используемых элементов не было — были хексы
россыпью и `.link-btn` вместо кнопки.

Правило экранирования (инвариант И-6) закреплено в именах:
  * параметр `text`, `label`, `value` — ПРОСТОЙ ТЕКСТ, функция экранирует сама;
  * параметр с суффиксом `_html` — уже готовая разметка, вызывающий отвечает за неё.

Никаких внешних ресурсов: ни шрифтов, ни CDN, ни картинок. Страница живёт в iframe
портала, любой внешний хост — лишний повод для отказа и лишняя утечка адреса.
Иконки — инлайновый SVG в одном стиле (обводка 1.5, currentColor).
"""
from __future__ import annotations

from b24bot.core.text import esc_attr, esc_html

# --------------------------------------------------------------------- палитра
#
# Токены, а не хексы по месту. Светлая и тёмная темы описаны парой: у портала
# есть тёмное оформление, и страница в iframe обязана его поддержать, иначе она
# светит белым прямоугольником посреди тёмного интерфейса.
#
# Контраст проверен: текст #101828 на #ffffff — 17:1; вторичный #475467 — 8.6:1;
# приглушённый #667085 — 5.6:1; белый на кнопке #2563eb — 5.2:1 (AA для обычного
# текста). В тёмной теме фон кнопки НЕ осветляется: белый на #3b82f6 даёт 3.7:1
# и провалил бы AA. Осветляется только цвет ссылок — им фон не нужен.
TOKENS = """
:root{
  --bg:#f2f4f7; --surface:#ffffff; --surface-2:#f9fafb; --surface-3:#f2f4f7;
  --border:#e4e7ec; --border-strong:#d0d5dd;
  --text:#101828; --text-2:#475467; --text-3:#667085;
  --primary:#2563eb; --primary-hover:#1d4ed8; --primary-fg:#ffffff;
  --accent:#175cd3;
  --ok:#067647; --ok-bg:#ecfdf3; --ok-border:#abefc6;
  --warn:#b54708; --warn-bg:#fffaeb; --warn-border:#fedf89;
  --err:#b42318; --err-bg:#fef3f2; --err-border:#fecdca;
  --info:#175cd3; --info-bg:#eff8ff; --info-border:#b2ddff;
  --shadow:0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.10);
  --shadow-lg:0 4px 8px -2px rgba(16,24,40,.10), 0 2px 4px -2px rgba(16,24,40,.06);
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:20px; --s6:24px; --s7:32px;
  --r1:6px; --r2:8px; --r3:12px; --r4:999px;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0d1117; --surface:#161b22; --surface-2:#1c232b; --surface-3:#222b35;
    --border:#2a333d; --border-strong:#3a4552;
    --text:#e6edf3; --text-2:#a9b6c3; --text-3:#8494a3;
    --primary:#2563eb; --primary-hover:#1d4ed8; --primary-fg:#ffffff;
    --accent:#7cc4fa;
    --ok:#75e0a7; --ok-bg:#0b2c1d; --ok-border:#1a5236;
    --warn:#fdb022; --warn-bg:#2e1c05; --warn-border:#5a3a0c;
    --err:#fda29b; --err-bg:#2f120e; --err-border:#5d2620;
    --info:#84caff; --info-bg:#0d233f; --info-border:#1c4272;
    --shadow:0 1px 2px rgba(0,0,0,.35); --shadow-lg:0 6px 16px rgba(0,0,0,.45);
  }
}
"""

CSS = TOKENS + """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
/* Панели вкладок скрываются атрибутом hidden — правило страхует его от
   переопределения любым display у элемента. */
[hidden]{display:none !important}
body{
  margin:0; padding:var(--s5) var(--s4) var(--s7);
  background:var(--bg); color:var(--text);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
       "Helvetica Neue",Arial,sans-serif;
  font-feature-settings:"kern" 1;
  -webkit-font-smoothing:antialiased;
}
/* Цифры в счётчиках и статусах не должны прыгать при обновлении. */
.tnum,.stat-v,.count{font-variant-numeric:tabular-nums}
.shell{max-width:980px;margin:0 auto}
p{margin:0 0 var(--s3);max-width:72ch}
p:last-child{margin-bottom:0}
h1,h2,h3{margin:0;font-weight:600;letter-spacing:-.01em}
h1{font-size:22px;line-height:1.25}
h2{font-size:16px;line-height:1.35}
h3{font-size:13px;line-height:1.4;color:var(--text-2);
   text-transform:uppercase;letter-spacing:.04em}
a{color:var(--accent)}
code{
  font:12px/1.4 ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  background:var(--surface-3); color:var(--text);
  padding:2px 6px; border-radius:var(--r1); word-break:break-all;
}
svg{flex:none;display:block}

/* --------------------------------------------------------------- заголовок */
.head{display:flex;align-items:flex-start;gap:var(--s4);
      justify-content:space-between;flex-wrap:wrap;margin:0 0 var(--s4)}
.head-id{display:flex;align-items:center;gap:var(--s3);min-width:0}
.mark{width:36px;height:36px;border-radius:var(--r2);flex:none;
      background:linear-gradient(135deg,#2563eb,#1e40af);color:#fff;
      display:flex;align-items:center;justify-content:center}
.head-t{min-width:0}
.head-sub{color:var(--text-3);font-size:12.5px;margin-top:2px;
          display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap}

/* ------------------------------------------------------------------ панели */
.panel{background:var(--surface);border:1px solid var(--border);
       border-radius:var(--r3);box-shadow:var(--shadow);margin:0 0 var(--s4);
       /* Дети с фоном (шапки чатов) не должны торчать из скруглённых углов. */
       overflow:hidden}
.panel-note{color:var(--text-3);font-size:12.5px}
.panel:last-child{margin-bottom:0}
.panel-h{display:flex;align-items:center;gap:var(--s3);justify-content:space-between;
         padding:var(--s4) var(--s5);border-bottom:1px solid var(--border);flex-wrap:wrap}
.panel-h-l{display:flex;align-items:center;gap:var(--s2);min-width:0}
.panel-b{padding:var(--s5)}
.panel-b.flush{padding:0}
.panel-f{padding:var(--s3) var(--s5);border-top:1px solid var(--border);
         background:var(--surface-2);border-radius:0 0 var(--r3) var(--r3)}

/* ------------------------------------------------------------------ статусы */
.badge{display:inline-flex;align-items:center;gap:6px;flex:none;
       padding:3px 9px 3px 7px;border-radius:var(--r4);
       font-size:12px;font-weight:500;line-height:1.4;white-space:nowrap;
       border:1px solid transparent}
/* Точка — второй, нецветовой признак: статус читается и в ч/б, и при дальтонизме. */
.badge::before{content:"";width:6px;height:6px;border-radius:50%;
               background:currentColor;flex:none}
.badge.ok{color:var(--ok);background:var(--ok-bg);border-color:var(--ok-border)}
.badge.warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn-border)}
.badge.err{color:var(--err);background:var(--err-bg);border-color:var(--err-border)}
.badge.info{color:var(--info);background:var(--info-bg);border-color:var(--info-border)}
.badge.neutral{color:var(--text-2);background:var(--surface-3);border-color:var(--border)}

/* ------------------------------------------------------------- строки полей */
.field{display:flex;gap:var(--s4);justify-content:space-between;align-items:center;
       padding:9px 0;border-bottom:1px solid var(--border);min-height:38px}
.field:first-child{padding-top:0}
.field:last-child{border-bottom:0;padding-bottom:0}
.field-k{color:var(--text-3);flex:none}
.field-v{text-align:right;min-width:0;overflow-wrap:anywhere}

/* -------------------------------------------------------------- плитки цифр */
.stats{display:grid;gap:var(--s3);
       grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.stat{padding:var(--s3) var(--s4);background:var(--surface-2);
      border:1px solid var(--border);border-radius:var(--r2)}
.stat-v{font-size:22px;font-weight:600;line-height:1.2;letter-spacing:-.02em}
.stat-l{color:var(--text-3);font-size:12.5px;margin-top:2px}

/* ------------------------------------------------------------------ списки */
.list{list-style:none;margin:0;padding:0}
.item{display:flex;gap:var(--s3);align-items:center;justify-content:space-between;
      padding:var(--s3) var(--s5);border-bottom:1px solid var(--border);flex-wrap:wrap}
.item:last-child{border-bottom:0}
.item-m{min-width:0;flex:1 1 260px}
.item-t{font-weight:500;overflow-wrap:anywhere}
.item-s{color:var(--text-3);font-size:12.5px;margin-top:2px;overflow-wrap:anywhere}
/* wrap обязателен: в строке участника рядом стоят два статуса и кнопка
   «Назначить админом», и на 375px они втроём не помещаются — без переноса
   кнопка уезжала за правый край и тянула горизонтальный скролл всей страницы. */
.item-a{display:flex;gap:var(--s2);align-items:center;flex:none;flex-wrap:wrap}
/* Чат — раздел списка: шапка на подложке, под ней проекты и свёрнутая форма.
   Раньше вложенность объяснялась линией слева, а форма была раскрыта всегда —
   вместе это читалось как каша из строк без границ. */
.chat-block{border-bottom:1px solid var(--border)}
.chat-block:last-child{border-bottom:0}
.chat-h{display:flex;gap:var(--s2) var(--s3);align-items:center;
        justify-content:space-between;flex-wrap:wrap;
        padding:10px var(--s5);background:var(--surface-2)}
.chat-meta{display:flex;flex-direction:column;gap:1px;min-width:0}
.chat-name{font-weight:600;overflow-wrap:anywhere}
.chat-id{color:var(--text-3);font-size:11.5px;
         font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace}
.chat-body{padding:var(--s2) var(--s5) var(--s4)}
.proj{display:flex;gap:var(--s3);align-items:center;justify-content:space-between;
      padding:9px 0;flex-wrap:wrap}
.proj + .proj{border-top:1px solid var(--border)}
.proj-m{display:flex;gap:10px;align-items:flex-start;min-width:0;flex:1 1 240px}
.proj-ico{color:var(--text-3);flex:none;margin-top:2px}
.proj-t{font-weight:500;overflow-wrap:anywhere}
.proj-s{color:var(--text-3);font-size:12.5px;margin-top:1px}
.proj-none{color:var(--text-3);font-size:13px;padding:9px 0}

/* Форма привязки за <details>: раскрытая на каждом чате, она заслоняла чаты. */
details.bind{margin-top:var(--s2)}
details.bind>summary{list-style:none;cursor:pointer;user-select:none;
  display:inline-flex;align-items:center;gap:7px;min-height:38px;padding:0 14px;
  border:1px dashed var(--border-strong);border-radius:var(--r2);
  color:var(--text-2);font-size:13.5px;font-weight:500;
  transition:background-color .15s ease,color .15s ease,border-color .15s ease}
details.bind>summary::-webkit-details-marker{display:none}
details.bind>summary:hover{background:var(--surface-2);color:var(--text)}
details.bind[open]>summary{border-style:solid;background:var(--surface-2);
  color:var(--text);margin-bottom:var(--s4)}

/* ------------------------------------------------------------------- кнопки */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
     min-height:38px;padding:0 14px;border-radius:var(--r2);
     border:1px solid transparent;background:var(--primary);color:var(--primary-fg);
     font:inherit;font-size:13.5px;font-weight:500;cursor:pointer;
     text-decoration:none;white-space:nowrap;
     transition:background-color .15s ease,border-color .15s ease,
                color .15s ease,transform .1s ease}
.btn:hover{background:var(--primary-hover)}
.btn:active{transform:translateY(1px)}
.btn.sec{background:var(--surface);color:var(--text);border-color:var(--border-strong)}
.btn.sec:hover{background:var(--surface-2);border-color:var(--text-3)}
.btn.ghost{background:transparent;color:var(--text-2);border-color:transparent;
           padding:0 10px;min-height:32px;font-size:13px}
.btn.ghost:hover{background:var(--surface-3);color:var(--text)}
/* Опасное действие красное и отделено от обычных — его нельзя нажать «заодно». */
.btn.danger{background:transparent;color:var(--err);border-color:transparent;
            padding:0 10px;min-height:32px;font-size:13px}
.btn.danger:hover{background:var(--err-bg);border-color:var(--err-border)}
.btn.wide{width:100%}
.btn[disabled]{opacity:.45;cursor:not-allowed;transform:none}
.btn[disabled]:hover{background:var(--primary);border-color:transparent}
.btn.sec[disabled]:hover{background:var(--surface)}
.btn.ghost[disabled]:hover,.btn.danger[disabled]:hover{background:transparent}
/* Фокус виден всегда и одинаково — обводку не снимаем ни у одного элемента. */
:focus-visible{outline:2px solid var(--primary);outline-offset:2px;border-radius:var(--r1)}
.btn-row{display:flex;gap:var(--s2);flex-wrap:wrap;align-items:center}
form.inline{display:inline-flex;margin:0}

/* ------------------------------------------------------------------- формы */
.f-group{margin:0 0 var(--s4)}
.f-group:last-child{margin-bottom:0}
label.f-l{display:block;font-size:13px;font-weight:500;color:var(--text-2);
          margin:0 0 6px}
.input,select.input,textarea.input{
  width:100%;min-height:42px;padding:9px 12px;
  border:1px solid var(--border-strong);border-radius:var(--r2);
  background:var(--surface);color:var(--text);
  /* Раздельные свойства, а не сокращение `font`: в сокращении `inherit` не
     является допустимым семейством, вся декларация отбрасывается целиком, и
     поле молча получает 13px от браузера.
     16px здесь не ради красоты — при меньшем размере Safari на iOS зумит
     страницу при фокусе, и портал в iframe уезжает вбок. */
  font-family:inherit;font-size:16px;line-height:1.4;
  transition:border-color .15s ease,box-shadow .15s ease}
.input:focus{outline:none;border-color:var(--primary);
             box-shadow:0 0 0 3px rgba(37,99,235,.15)}
.input.mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
            font-size:16px}
textarea.input{resize:vertical;line-height:1.5;min-height:96px}
/* Стрелка списка нарисована инлайном: внешних картинок на странице нет вообще.
   Обратные слэши — перенос строки в Python, в CSS уезжает одна длинная строка. */
select.input{appearance:none;padding-right:34px;cursor:pointer;
  background-image:url("data:image/svg+xml;charset=utf-8,\
%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' fill='none' \
stroke='%23667085' stroke-width='1.5' stroke-linecap='round' \
stroke-linejoin='round'%3E%3Cpath d='m4 6 4 4 4-4'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 11px center}
.grid2{display:grid;gap:var(--s3);grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.hint{color:var(--text-3);font-size:12.5px;line-height:1.5;margin-top:var(--s2);
      max-width:72ch}
.hint:first-child{margin-top:0}

/* ---------------------------------------------------------------- сообщения */
.banner{display:flex;gap:var(--s3);align-items:flex-start;
        padding:var(--s3) var(--s4);border-radius:var(--r2);
        border:1px solid;margin:0 0 var(--s4);font-size:13.5px}
.banner p{max-width:none}
.banner.ok{color:var(--ok);background:var(--ok-bg);border-color:var(--ok-border)}
.banner.warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn-border)}
.banner.err{color:var(--err);background:var(--err-bg);border-color:var(--err-border)}
.banner.info{color:var(--info);background:var(--info-bg);border-color:var(--info-border)}
.banner b{font-weight:600}
.banner-b{min-width:0;overflow-wrap:anywhere}

/* ------------------------------------------------------------------- пусто */
.empty{text-align:center;padding:var(--s7) var(--s5);max-width:56ch;margin:0 auto}
.empty-i{width:44px;height:44px;border-radius:var(--r3);margin:0 auto var(--s3);
         background:var(--surface-3);color:var(--text-3);
         display:flex;align-items:center;justify-content:center}
.empty-t{font-weight:600;font-size:15px;margin:0 0 var(--s1)}
.empty-x{color:var(--text-3);font-size:13.5px;margin:0 auto var(--s4);max-width:48ch}

/* --------------------------------------------------------------- онбординг */
.steps{list-style:none;margin:0;padding:0;counter-reset:step}
.step{display:flex;gap:var(--s4);padding:var(--s4) var(--s5);
      border-bottom:1px solid var(--border)}
.step:last-child{border-bottom:0}
.step-n{width:26px;height:26px;border-radius:50%;flex:none;
        display:flex;align-items:center;justify-content:center;
        font-size:12.5px;font-weight:600;
        background:var(--surface-3);color:var(--text-3);
        border:1px solid var(--border)}
.step.now .step-n{background:var(--primary);color:var(--primary-fg);
                  border-color:var(--primary)}
.step.done .step-n{background:var(--ok-bg);color:var(--ok);border-color:var(--ok-border)}
.step-b{min-width:0;flex:1}
.step-t{font-weight:600;display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap}
.step.done .step-t{color:var(--text-3);font-weight:500}
.step-x{color:var(--text-3);font-size:13px;margin-top:2px;max-width:64ch}
.step-a{margin-top:var(--s3)}

/* -------------------------------------------------------------------- табы */
.tabs{display:flex;gap:var(--s1);margin:0 0 var(--s4);padding:var(--s1);
      background:var(--surface-3);border-radius:var(--r2);
      overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{display:inline-flex;align-items:center;gap:7px;flex:1 0 auto;
     justify-content:center;min-height:36px;padding:0 14px;
     border:0;background:transparent;color:var(--text-2);
     font:inherit;font-size:13.5px;font-weight:500;cursor:pointer;
     border-radius:var(--r1);white-space:nowrap;
     transition:background-color .15s ease,color .15s ease}
.tab:hover{color:var(--text)}
.tab[aria-selected="true"]{background:var(--surface);color:var(--text);
                           box-shadow:var(--shadow)}
.tab .count{display:inline-flex;align-items:center;justify-content:center;
            min-width:19px;height:19px;padding:0 5px;border-radius:var(--r4);
            background:var(--surface-3);color:var(--text-3);
            font-size:11.5px;font-weight:600}
.tab[aria-selected="true"] .count{background:var(--primary);color:var(--primary-fg)}
.tab .dot{width:7px;height:7px;border-radius:50%;background:var(--err);flex:none}

/* ------------------------------------------------------------------ мелочи */
.muted{color:var(--text-3)}
.divider{height:1px;background:var(--border);margin:var(--s5) 0}
.stack{display:flex;flex-direction:column;gap:var(--s3)}
.row-wrap{display:flex;gap:var(--s3);align-items:center;flex-wrap:wrap}

/* -------------------------------------------------------------- адаптивность */
@media (max-width:640px){
  body{padding:var(--s3) var(--s3) var(--s6);font-size:15px}
  .panel-b,.panel-h,.item,.step{padding-left:var(--s4);padding-right:var(--s4)}
  .chat-h,.chat-body{padding-left:var(--s4);padding-right:var(--s4)}
  .field{flex-direction:column;align-items:flex-start;gap:2px}
  .field-v{text-align:left}
  .item-a{width:100%;justify-content:flex-start}
  h1{font-size:19px}
}
/* На тач-экранах (мобильный Битрикс24) КАЖДАЯ цель дорастает до 44px —
   это минимум из Apple HIG, а не пожелание. Компактные варианты кнопок
   здесь теряют компактность намеренно: промах по «Отвязать» дороже. */
@media (pointer:coarse){
  .btn,.btn.ghost,.btn.danger{min-height:44px}
  .btn.ghost,.btn.danger{padding:0 12px}
  .tab{min-height:44px}
  .input,select.input{min-height:44px}
  details.bind>summary{min-height:44px}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms !important;
    transition-duration:.01ms !important;scroll-behavior:auto !important}
}
"""


# ---------------------------------------------------------------------- иконки
#
# Один набор, одна обводка, currentColor. Эмодзи в роли иконок не используются:
# они зависят от шрифта системы, не подчиняются теме и выглядят по-разному
# в Windows, macOS и Android.
_PATHS: dict[str, str] = {
    "send": "m21 3-9 18-2-7-7-2Z M21 3 10 14",
    "check": "M20 6 9 17l-5-5",
    "check-circle": "M22 11.1V12a10 10 0 1 1-5.9-9.1 M22 4 12 14.1l-3-3",
    "alert": "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"
             " M12 9v4 M12 17h.01",
    "x-circle": "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z M15 9l-6 6 M9 9l6 6",
    "info": "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z M12 16v-4 M12 8h.01",
    "chat": "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z",
    "folder": "M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9"
              "A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z",
    "users": "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z"
             " M22 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75",
    "user": "M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2 M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z",
    "shield": "M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1"
              " 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0"
              " 0 1 1 1Z",
    "refresh": "M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8 M21 3v5h-5"
               " M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16 M8 16H3v5",
    "link": "M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"
            " M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71",
    "unlink": "M18.84 12.25l1.72-1.71a5 5 0 0 0-7.07-7.07l-1.72 1.71 M5.17 11.75l-1.71 1.71a5"
              " 5 0 0 0 7.07 7.07l1.71-1.71 M8 2v3 M2 8h3 M16 22v-3 M22 16h-3",
    "plus": "M12 5v14 M5 12h14",
    "external": "M15 3h6v6 M10 14 21 3 M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6",
    "settings": "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06"
                "a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51"
                "V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06"
                "a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1"
                "H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06"
                "a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3"
                "a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06"
                "a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1"
                "H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z",
    "inbox": "M22 12h-6l-2 3h-4l-2-3H2 M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6"
             "l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11Z",
    "arrow-up": "M12 19V5 M5 12l7-7 7 7",
    "arrow-down": "M12 5v14 M19 12l-7 7-7-7",
    "arrow-left": "M19 12H5 M12 19l-7-7 7-7",
}


def icon(name: str, size: int = 16) -> str:
    """Инлайновый SVG. Неизвестное имя даёт пустую строку, а не битую разметку."""
    path = _PATHS.get(name)
    if path is None:
        return ""
    shapes = ""
    for chunk in path.split(" M"):
        d = chunk.strip()
        if not d:
            continue
        if d[0] not in "Mm":
            d = "M" + d
        shapes += f'<path d="{d}"/>'
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true" focusable="false">{shapes}</svg>')


# ------------------------------------------------------------------ компоненты
def badge(text: str, kind: str = "neutral") -> str:
    """Статус: цвет + точка + слово. Никогда только цветом (правило доступности)."""
    kind = kind if kind in ("ok", "warn", "err", "info", "neutral") else "neutral"
    return f'<span class="badge {kind}">{esc_html(text)}</span>'


def banner(body_html: str, kind: str = "ok") -> str:
    """Результат действия. `aria-live` — чтобы скринридер объявил, не забирая фокус."""
    kind = kind if kind in ("ok", "warn", "err", "info") else "info"
    ico = {"ok": "check-circle", "warn": "alert", "err": "x-circle", "info": "info"}[kind]
    return (f'<div class="banner {kind}" role="status" aria-live="polite">'
            f'{icon(ico, 18)}<div class="banner-b">{body_html}</div></div>')


def panel(title: str, body_html: str, *, icon_name: str = "", actions_html: str = "",
          footer_html: str = "", flush: bool = False, id_attr: str = "") -> str:
    """Секция экрана. Заголовок обязателен: панель без имени — это просто рамка."""
    head = ""
    if title:
        left = (f'<div class="panel-h-l">{icon(icon_name, 18) if icon_name else ""}'
                f"<h2>{esc_html(title)}</h2></div>")
        head = f'<div class="panel-h">{left}{actions_html}</div>'
    foot = f'<div class="panel-f">{footer_html}</div>' if footer_html else ""
    cls = "panel-b flush" if flush else "panel-b"
    ident = f' id="{esc_attr(id_attr)}"' if id_attr else ""
    return (f'<section class="panel"{ident}>{head}'
            f'<div class="{cls}">{body_html}</div>{foot}</section>')


def field(label: str, value_html: str) -> str:
    """Строка «свойство — значение»."""
    return (f'<div class="field"><span class="field-k">{esc_html(label)}</span>'
            f'<span class="field-v">{value_html}</span></div>')


def stat(value: str, label: str) -> str:
    return (f'<div class="stat"><div class="stat-v">{esc_html(value)}</div>'
            f'<div class="stat-l">{esc_html(label)}</div></div>')


def stats(items: list[tuple[str, str]]) -> str:
    tiles = "".join(stat(value, label) for value, label in items)
    return f'<div class="stats">{tiles}</div>'


def empty(title: str, text: str, *, action_html: str = "", icon_name: str = "inbox") -> str:
    """Пустое состояние обязано объяснить, что сделать. Иначе это тупик."""
    act = f'<div class="btn-row" style="justify-content:center">{action_html}</div>' \
        if action_html else ""
    return (f'<div class="empty"><div class="empty-i">{icon(icon_name, 22)}</div>'
            f'<div class="empty-t">{esc_html(title)}</div>'
            f'<p class="empty-x">{esc_html(text)}</p>{act}</div>')


def hint(text: str) -> str:
    return f'<p class="hint">{esc_html(text)}</p>'


def hint_html(body_html: str) -> str:
    return f'<p class="hint">{body_html}</p>'


def action_form(url: str, fields: dict[str, str | int], label: str, *,
                variant: str = "ghost", icon_name: str = "", confirm: str = "",
                disabled: bool = False, title: str = "", inline: bool = True) -> str:
    """Кнопка-действие вместе с её формой.

    Раньше каждое такое действие занимало пять строк разметки в обработчике и
    рисовалось `.link-btn` — подчёркнутым текстом 12px без состояния наведения
    и без цели для пальца. Здесь это одна вызываемая функция и настоящая кнопка.

    `confirm` вешает подтверждение на отправку: снятие привязки и снятие прав
    необратимы одним кликом (правило confirmation-dialogs).
    """
    hidden = "".join(
        f'<input type="hidden" name="{esc_attr(k)}" value="{esc_attr(v)}">'
        for k, v in fields.items())
    attrs = ""
    if disabled:
        attrs += " disabled"
    if title:
        attrs += f' title="{esc_attr(title)}"'
    if confirm:
        attrs += f' data-confirm="{esc_attr(confirm)}"'
    ico = icon(icon_name, 15) if icon_name else ""
    cls = "inline" if inline else ""
    return (f'<form method="post" action="{esc_attr(url)}" class="{cls}">{hidden}'
            f'<button class="btn {variant}" type="submit"{attrs}>{ico}'
            f"{esc_html(label)}</button></form>")


def goto_button(tab: str, label: str, *, variant: str = "", icon_name: str = "") -> str:
    """Кнопка перехода на другую вкладку.

    Шаг мастера, который только рассказывает, куда пойти, заставляет человека
    искать это место самому. Кнопка переносит туда сразу.
    """
    ico = icon(icon_name, 15) if icon_name else ""
    return (f'<button type="button" class="btn {variant}" '
            f'data-goto="{esc_attr(tab)}">{ico}{esc_html(label)}</button>')


def link_button(url: str, label: str, *, variant: str = "", icon_name: str = "",
                new_tab: bool = True) -> str:
    tgt = ' target="_blank" rel="noopener"' if new_tab else ""
    ico = icon(icon_name, 15) if icon_name else ""
    return (f'<a class="btn {variant}" href="{esc_attr(url)}"{tgt}>{ico}'
            f"{esc_html(label)}</a>")


def _row_inner(title_html: str, sub_html: str, actions_html: str) -> str:
    sub = f'<div class="item-s">{sub_html}</div>' if sub_html else ""
    act = f'<div class="item-a">{actions_html}</div>' if actions_html else ""
    return (f'<div class="item-m"><div class="item-t">{title_html}</div>{sub}</div>{act}')


def item(title_html: str, *, sub_html: str = "", actions_html: str = "") -> str:
    """Строка НАСТОЯЩЕГО списка — только внутри <ul>."""
    return f'<li class="item">{_row_inner(title_html, sub_html, actions_html)}</li>'


def row(title_html: str, *, sub_html: str = "", actions_html: str = "") -> str:
    """Та же строка, но вне списка.

    Нужна там, где элемент один: список из одного пункта скринридер объявляет
    как «список, 1 элемент» перед каждым чатом, и это чистый шум.
    """
    return f'<div class="item">{_row_inner(title_html, sub_html, actions_html)}</div>'


def step(number: int, title: str, text: str, *, state: str = "todo",
         action_html: str = "") -> str:
    """Шаг мастера первичной настройки.

    Готовый шаг помечается галочкой, а не только цветом кружка.
    """
    cls = {"done": "step done", "now": "step now"}.get(state, "step")
    mark = icon("check", 14) if state == "done" else str(number)
    tail = badge("готово", "ok") if state == "done" else ""
    # Кнопка только у текущего шага: три кнопки разом — это три главных
    # действия на экране, то есть ни одного.
    act = f'<div class="step-a">{action_html}</div>' if action_html and state == "now" else ""
    return (f'<li class="{cls}"><div class="step-n">{mark}</div>'
            f'<div class="step-b"><div class="step-t">{esc_html(title)}{tail}</div>'
            f'<div class="step-x">{esc_html(text)}</div>{act}</div></li>')


# --------------------------------------------------------------------- скрипты
#
# Три задачи, каждая закрывает конкретную поломку:
#   1. fitWindow по ResizeObserver — раньше вызывался один раз при загрузке, и
#      любое изменение высоты оставляло iframe неверного размера;
#   2. вкладки без перезагрузки — состояние переносится через скрытое поле в
#      каждой форме, поэтому после POST человек остаётся там, где работал;
#   3. подтверждение необратимых действий.
SCRIPT = """
(function(){
  var root = document.documentElement;

  function fit(){
    if (window.BX24 && BX24.fitWindow) { try { BX24.fitWindow(); } catch(e){} }
  }
  if (window.BX24 && BX24.init) { BX24.init(fit); } else { fit(); }

  if (window.ResizeObserver) {
    var t = null;
    new ResizeObserver(function(){
      clearTimeout(t); t = setTimeout(fit, 60);
    }).observe(document.body);
  }

  var tabs = [].slice.call(document.querySelectorAll('[role="tab"]'));
  var panels = [].slice.call(document.querySelectorAll('[role="tabpanel"]'));

  function activate(name, focus){
    tabs.forEach(function(tab){
      var on = tab.getAttribute('data-tab') === name;
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      tab.tabIndex = on ? 0 : -1;
      if (on && focus) tab.focus();
    });
    panels.forEach(function(p){
      p.hidden = p.getAttribute('data-panel') !== name;
    });
    // Все формы возвращают активную вкладку на сервер: после перезагрузки
    // страница открывается на том же разделе, а не прыгает в начало.
    document.querySelectorAll('input[name="tab"]').forEach(function(i){ i.value = name; });
    fit();
  }

  tabs.forEach(function(tab, i){
    tab.addEventListener('click', function(){ activate(tab.getAttribute('data-tab')); });
    tab.addEventListener('keydown', function(e){
      var d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      var next = tabs[(i + d + tabs.length) % tabs.length];
      activate(next.getAttribute('data-tab'), true);
    });
  });

  // Кнопки «перейти к шагу» из мастера настройки.
  document.querySelectorAll('[data-goto]').forEach(function(b){
    b.addEventListener('click', function(){
      activate(b.getAttribute('data-goto'));
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });

  document.addEventListener('submit', function(e){
    var btn = e.submitter || e.target.querySelector('[data-confirm]');
    var ask = btn && btn.getAttribute && btn.getAttribute('data-confirm');
    if (ask && !window.confirm(ask)) { e.preventDefault(); return; }
    // Двойная отправка формы создавала вторую привязку и второй запрос в портал.
    if (btn && btn.tagName === 'BUTTON') {
      setTimeout(function(){ btn.disabled = true; btn.style.opacity = '.6'; }, 0);
    }
  });

  root.classList.add('ready');
})();
"""
