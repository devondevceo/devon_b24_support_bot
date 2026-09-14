/**
 * Telegram не привязан к Битрикс24 — первый экран у каждого нового человека.
 *
 * Отдельным компонентом, а не разметкой внутри App: так его меряет стенд
 * аудита, а раньше этот экран проверял только глаз.
 *
 * Путей привязки два, и назван первым тот, что начинается там, где человек
 * уже есть, — в Telegram (`/link`, docs/30-bot-spec.md §6.1.1). До 14.09.2026
 * здесь был описан только путь через портал, хотя бот с 27.08 зовёт к `/link`:
 * два места подсказывали одно и то же по-разному.
 */
import { Empty } from '../components/States'
import { Icon } from '../ui/Icon'

export function NotLinkedScreen({ onRetry, onGuide }: { onRetry: () => void; onGuide: () => void }) {
  return (
    <Empty
      icon="link"
      title="Telegram не привязан к Битрикс24"
      hint={
        <>
          Отправьте боту <code>/link</code> в личном чате и войдите в свой портал — это
          полминуты. Или откройте в Битрикс24 приложение «Поддержка в Telegram» и нажмите
          «Привязать Telegram».
        </>
      }
      action={
        <div className="actions stack state-actions">
          <button type="button" className="btn" onClick={onRetry}>
            <Icon name="refresh" size={18} />
            Я привязал, проверить
          </button>
          <button type="button" className="btn sec" onClick={onGuide}>
            <Icon name="book" size={18} />
            Как привязать
          </button>
        </div>
      }
    />
  )
}
