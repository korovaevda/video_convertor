#!/bin/bash

# Проверка количества аргументов
if [ "$#" -lt 2 ]; then
    echo "Использование: $0 <количество_файлов> <файл_музыки> [fade_duration]"
    echo "Пример: $0 8 background_music.mp3 3"
    echo ""
    echo "Параметры:"
    echo "  <количество_файлов> - количество видео для обработки (файлы должны быть 001.mp4, 002.mp4, ...)"
    echo "  <файл_музыки>       - путь к файлу фоновой музыки"
    echo "  [fade_duration]     - длительность затухания в секундах (по умолчанию 3)"
    exit 1
fi

# Входные параметры
NUM_FILES="$1"
MUSIC="$2"
FADE_DURATION="${3:-3}"

# Проверка существования музыкального файла
if [ ! -f "$MUSIC" ]; then
    echo "Ошибка: Файл $MUSIC не найден!"
    exit 1
fi

# Начало отсчёта времени
START_TIME=$(date +%s)

echo "=========================================="
echo "Начало обработки видео"
echo "=========================================="
echo "Количество файлов: $NUM_FILES"
echo "Музыка: $MUSIC"
echo "Длительность затухания: $FADE_DURATION сек"
echo "Время начала: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Шаг 1: Масштабирование и создание списка
echo "Шаг 1/4: Масштабирование видео к 1080x1920..."
> filelist.txt
for ((i=1; i<=NUM_FILES; i++)); do
    printf -v file_num "%03d" "$i"
    input_file="${file_num}.mp4"
    
    if [ ! -f "$input_file" ]; then
        echo "Предупреждение: Файл $input_file не найден, пропускаем..."
        continue
    fi
    
    echo "Обработка $input_file..."
    ffmpeg -i "$input_file" -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black" -c:a copy "${file_num}_scaled.mp4" -y -loglevel error
    
    if [ $? -eq 0 ]; then
        echo "file '${file_num}_scaled.mp4'" >> filelist.txt
    else
        echo "Ошибка при обработке $input_file"
        exit 1
    fi
done

# Проверка, что есть файлы для склейки
if [ ! -s filelist.txt ]; then
    echo "Ошибка: Нет файлов для склейки!"
    exit 1
fi

echo ""
echo "Шаг 2/4: Склейка видео..."
ffmpeg -f concat -safe 0 -i filelist.txt -c copy temp_concat.mp4 -y -loglevel error

if [ $? -ne 0 ]; then
    echo "Ошибка при склейке видео!"
    exit 1
fi

echo ""
echo "Шаг 3/4: Добавление музыки с fade out..."

# Получаем длительность видео и аудио
video_duration=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 temp_concat.mp4)
audio_duration=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$MUSIC")

echo "Длительность видео: ${video_duration%.*} сек"
echo "Длительность аудио: ${audio_duration%.*} сек"

# Вычисляем начало fade out
fade_start=$(echo "$video_duration - $FADE_DURATION" | bc)
echo "Fade out начнётся с: ${fade_start%.*} сек"

# Проверяем, нужно ли зациклить аудио
if (( $(echo "$audio_duration < $video_duration" | bc -l) )); then
    echo "Аудио короче видео, создаём зацикленную версию..."
    
    # Создаём зацикленный аудиофайл нужной длины
    ffmpeg -stream_loop -1 -i "$MUSIC" -t $video_duration -c:a aac temp_looped_audio.m4a -y -loglevel error
    
    echo "Добавление музыки с fade out..."
    # Добавляем зацикленное аудио с fade out
    ffmpeg -i temp_concat.mp4 -i temp_looped_audio.m4a \
        -filter_complex "[1:a]afade=t=out:st=$fade_start:d=$FADE_DURATION[music]" \
        -map 0:v -map "[music]" -c:v copy -shortest result.mp4 -y -loglevel error
    
    rm -f temp_looped_audio.m4a
else
    echo "Аудио длиннее или равно видео, добавление музыки с fade out..."
    
    # Просто добавляем музыку с fade out
    ffmpeg -i temp_concat.mp4 -i "$MUSIC" \
        -filter_complex "[1:a]afade=t=out:st=$fade_start:d=$FADE_DURATION[music]" \
        -map 0:v -map "[music]" -c:v copy -shortest result.mp4 -y -loglevel error
fi

if [ $? -ne 0 ]; then
    echo "Ошибка при добавлении музыки!"
    exit 1
fi

echo ""
echo "Шаг 4/4: Очистка временных файлов..."
rm -f *_scaled.mp4 filelist.txt temp_concat.mp4

# Конец отсчёта времени
END_TIME=$(date +%s)
ELAPSED_TIME=$((END_TIME - START_TIME))

# Форматирование времени выполнения
HOURS=$((ELAPSED_TIME / 3600))
MINUTES=$(((ELAPSED_TIME % 3600) / 60))
SECONDS=$((ELAPSED_TIME % 60))

echo ""
echo "=========================================="
echo "✅ Готово! Результат: result.mp4"
echo "=========================================="
echo "Время завершения: $(date '+%Y-%m-%d %H:%M:%S')"
if [ $HOURS -gt 0 ]; then
    echo "Время выполнения: ${HOURS}ч ${MINUTES}м ${SECONDS}с"
elif [ $MINUTES -gt 0 ]; then
    echo "Время выполнения: ${MINUTES}м ${SECONDS}с"
else
    echo "Время выполнения: ${SECONDS}с"
fi
echo "=========================================="
