# Copyright (C) 2025 GALINA Sébastien <galina.sebastien@gmail.om>
# 
# This file is part of FPVSoundLogger.
#
# FPVSoundLogger is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import os, time
import uasyncio as asyncio
from machine import Pin, I2S, SPI
from sdcard import SDCard
from neopixel import NeoPixel

class FpvSoundLogger:

    SOLID = 0
    BLINK = 1

    def __init__(self):
        # LED
        self.rgb_pin = 16
        self.led = NeoPixel(Pin(self.rgb_pin), 1)
        self.led_mode = self.SOLID
        self.led_color = (0,0,0)
        self.led_task = None

        # Pins
        self.enable_pin = Pin(1, Pin.IN, Pin.PULL_UP)
        self.trigger_pin = Pin(2, Pin.IN, Pin.PULL_UP)

        # I2S - 44.1 kHz Configuration
        self.i2s_id = 0
        self.i2s_sck = 27
        self.i2s_ws  = 28
        self.i2s_sd  = 26
        self.sample_rate = 22050
        self.bits = 16
        self.format = I2S.MONO
        self.buffer_length = 40000

        # SD
        self.sd_spi_id = 1
        self.sd_cs = 13
        self.sd_sck = 10
        self.sd_mosi = 11
        self.sd_miso = 12

        # Internal state
        self.is_recording = False
        self.stop_requested = False

        # File indexing
        self.last_index = 0
        self.current_index = 0

        # Double buffer
        self.buffer_size = 8192
        self.buf_a = bytearray(self.buffer_size)
        self.buf_b = bytearray(self.buffer_size)
        self.mv_a = memoryview(self.buf_a)
        self.mv_b = memoryview(self.buf_b)
        
        # Buffer state for producer-consumer pattern
        self.write_buffer = None  # Buffer to write to SD
        self.write_size = 0       # Size of data to write
        self.buffer_ready = False # Flag to signal data ready

    # ---------------------------------------------------------
    # Index management
    # ---------------------------------------------------------
    def _load_last_index(self):
        """Load the last used file index from SD card"""
        try:
            with open("/sd/last_index.txt", "r") as f:
                return int(f.read().strip())
        except:
            return 0

    def _save_last_index(self, index):
        """Save the current file index to SD card"""
        try:
            with open("/sd/last_index.txt", "w") as f:
                f.write(str(index))
        except:
            print("Error writing last_index.txt")

    # ---------------------------------------------------------
    # Led control
    # ---------------------------------------------------------
    async def led_loop(self):
        """Async task to handle LED blinking or solid colors"""
        while True:
            if self.led_mode == self.SOLID:
                self.led[0] = self.led_color
                self.led.write()
                await asyncio.sleep_ms(40)
            else:  # BLINK mode
                self.led[0] = self.led_color
                self.led.write()
                await asyncio.sleep_ms(200)
                self.led[0] = (0,0,0)
                self.led.write()
                await asyncio.sleep_ms(200)

    async def set_led(self, color_name, mode=SOLID):
        """Set LED color and mode (solid or blink)"""
        colors = {
            "red":    (10, 0, 0),
            "green":  (0, 10, 0),
            "blue":   (0, 0, 10),
            "yellow": (10, 10, 0),
            "off":    (0, 0, 0),
        }
        self.led_color = colors.get(color_name, (0,0,0))
        self.led_mode = mode

        # Cancel previous LED task and create new one
        if self.led_task:
            self.led_task.cancel()
            try:
                await self.led_task
            except asyncio.CancelledError:
                pass
        self.led_task = asyncio.create_task(self.led_loop())

    # ---------------------------------------------------------
    # Sd card initialization
    # ---------------------------------------------------------
    def init_sd(self):
        """Initialize SD card with SPI interface"""
        print("Mounting SD…")
        spi = SPI(
            self.sd_spi_id,
            baudrate=25_000_000,
            sck=Pin(self.sd_sck),
            mosi=Pin(self.sd_mosi),
            miso=Pin(self.sd_miso)
        )
        sd = SDCard(spi, Pin(self.sd_cs))
        os.mount(sd, "/sd")
        print("SD mounted.")

    # ---------------------------------------------------------
    # Audio (I2S) initialization
    # ---------------------------------------------------------
    def init_audio(self):
        """Initialize I2S microphone interface"""
        print("Initializing I2S audio…")
        self.i2s = I2S(
            self.i2s_id,
            sck=Pin(self.i2s_sck),
            ws=Pin(self.i2s_ws),
            sd=Pin(self.i2s_sd),
            mode=I2S.RX,
            bits=self.bits,
            format=self.format,
            rate=self.sample_rate,
            ibuf=self.buffer_length,
        )
        print("I2S ready.")

    # ---------------------------------------------------------
    # WAV header generation
    # ---------------------------------------------------------
    def write_wav_header(self, filename, sample_rate, bits_per_sample, channels, data_size):
        """
        Write a proper WAV header to an existing file.
        This is done after recording to set the correct file size.
        """
        byte_rate = sample_rate * channels * (bits_per_sample // 8)
        block_align = channels * (bits_per_sample // 8)

        with open(filename, "r+b") as f:
            # RIFF chunk
            f.write(b'RIFF')
            f.write((36 + data_size).to_bytes(4, 'little'))
            f.write(b'WAVE')

            # fmt subchunk
            f.write(b'fmt ')
            f.write((16).to_bytes(4, 'little'))          # Subchunk1Size (PCM)
            f.write((1).to_bytes(2, 'little'))           # AudioFormat (PCM)
            f.write(channels.to_bytes(2, 'little'))      # NumChannels
            f.write(sample_rate.to_bytes(4, 'little'))   # SampleRate
            f.write(byte_rate.to_bytes(4, 'little'))     # ByteRate
            f.write(block_align.to_bytes(2, 'little'))   # BlockAlign
            f.write(bits_per_sample.to_bytes(2, 'little')) # BitsPerSample

            # data subchunk
            f.write(b'data')
            f.write(data_size.to_bytes(4, 'little'))

    # ---------------------------------------------------------
    # Recording start
    # ---------------------------------------------------------
    def start_recording(self):
        """Start a new recording session"""
        if self.is_recording:
            return

        self.is_recording = True
        self.stop_requested = False

        # Generate filename with incremented index
        self.current_index = self.last_index + 1
        self.temp_filename = f"/sd/rec_{self.current_index:04d}_temp.wav"

        print(f"Starting recording: {self.temp_filename}")

        # Create file with placeholder header (44 bytes)
        self.wav = open(self.temp_filename, "wb")
        self.wav.write(b'\x00' * 44)
        self.wav.flush()

        # Reset counters
        self.total_bytes_written = 0
        self.start_time = time.time()

        # Reset buffer state
        self.buffer_ready = False
        self.write_buffer = None

        # Start async tasks
        asyncio.create_task(self.reader())
        asyncio.create_task(self.writer())

    # ---------------------------------------------------------
    # Recording stop
    # ---------------------------------------------------------
    def stop_recording(self):
        """Request to stop the current recording"""
        if self.is_recording:
            print("Requesting stop…")
            self.stop_requested = True

    # ---------------------------------------------------------
    # Reader task
    # ---------------------------------------------------------
    async def reader(self):
        """
        Async task that reads audio data from I2S microphone.
        Uses double buffering: alternates between buf_a and buf_b.
        """
        print("Reader started")
        current_buf = 0
        dropped_frames = 0
        total_reads = 0
        
        while not self.stop_requested:
            try:
                # Select current buffer
                if current_buf == 0:
                    buf = self.mv_a
                else:
                    buf = self.mv_b
                
                # Read audio data from I2S
                num_bytes = self.i2s.readinto(buf)
                total_reads += 1
                
                if num_bytes > 0:
                    # Check if writer is lagging (buffer overflow protection)
                    if self.buffer_ready:
                        dropped_frames += 1
                        if dropped_frames % 10 == 0:  # Print every 10 drops
                            print(f"⚠️ Buffer full! Dropped: {dropped_frames}/{total_reads}")
                    
                    # Wait until writer is ready
                    while self.buffer_ready and not self.stop_requested:
                        await asyncio.sleep_ms(0)
                    
                    # Pass buffer to writer
                    self.write_buffer = buf
                    self.write_size = num_bytes
                    self.buffer_ready = True
                    
                    # Switch to other buffer
                    current_buf = 1 - current_buf
                    await asyncio.sleep_ms(0)
                else:
                    await asyncio.sleep_ms(5)
                    
            except Exception as e:
                print("Error in reader:", e)
                await asyncio.sleep_ms(10)
        
        print(f"Reader stopped. Dropped: {dropped_frames}/{total_reads} ({100*dropped_frames/max(1,total_reads):.1f}%)")

    # ---------------------------------------------------------
    # Writer task
    # ---------------------------------------------------------
    async def writer(self):
        """
        Async task that writes audio data to SD card.
        Consumes buffers prepared by the reader task.
        """
        print("Writer started")
        
        while not self.stop_requested or self.buffer_ready:
            try:
                # Wait for buffer to be ready
                if not self.buffer_ready:
                    await asyncio.sleep_ms(0)
                    continue
                
                # Write buffer to SD card
                self.wav.write(self.write_buffer[:self.write_size])
                self.total_bytes_written += self.write_size
                
                # Release buffer
                self.buffer_ready = False
                
                # Yield for multitasking
                await asyncio.sleep_ms(0)
                
            except Exception as e:
                print("Error in writer:", e)
                self.buffer_ready = False
                await asyncio.sleep_ms(10)

        # ----- Finalize recording -----
        print(f"Finalizing... Total bytes: {self.total_bytes_written}")
        
        self.wav.flush()
        self.wav.close()

        # Write the correct WAV header with actual data size
        self.write_wav_header(
            self.temp_filename,
            self.sample_rate,
            self.bits,
            1,  # MONO
            self.total_bytes_written
        )

        # Calculate recording duration
        duration = int(time.time() - self.start_time)
        mm = duration // 60
        ss = duration % 60
        dur_str = f"{mm:02d}-{ss:02d}"

        # Rename file with duration
        old = self.temp_filename
        base = old.replace("_temp.wav", "")
        new_name = f"{base}_{dur_str}.wav"

        os.rename(old, new_name)

        # Update last index
        self.last_index = self.current_index
        self._save_last_index(self.last_index)

        self.is_recording = False

        print("Saved:", new_name)
        print("Index updated:", self.last_index)

    # ---------------------------------------------------------
    # Pin checking
    # ---------------------------------------------------------
    def check_pins(self):
        """
        Read enable and trigger pins.
        Returns: (enabled, triggered)
        """
        enabled = self.enable_pin.value() == 1
        triggered = self.trigger_pin.value() == 0
        return enabled, triggered

    # ---------------------------------------------------------
    # Monitor loop
    # ---------------------------------------------------------
    async def monitor(self):
        """
        Main monitoring loop that controls recording state based on pins.
        
        LED states:
        - Red blinking: System disabled
        - Green blinking: System enabled, waiting for trigger
        - Blue blinking: Recording in progress
        """
        await self.set_led("red", self.BLINK)

        while True:
            enabled, triggered = self.check_pins()

            if not enabled:
                # System disabled
                await self.set_led("red", self.BLINK)
                self.stop_recording()

            elif enabled and not triggered:
                # System enabled but not triggered
                await self.set_led("green", self.BLINK)
                self.stop_recording()

            elif enabled and triggered:
                # System enabled and triggered - start recording
                if not self.is_recording:
                    await self.set_led("blue", self.BLINK)
                    self.start_recording()

            await asyncio.sleep_ms(30)

    # ---------------------------------------------------------
    # Start logger
    # ---------------------------------------------------------
    def start(self):
        """
        Initialize the sound logger system.
        Call this once to start the whole system.
        """
        self.init_sd()
        self.last_index = self._load_last_index()
        print("Last index loaded:", self.last_index)

        self.init_audio()
        asyncio.create_task(self.monitor())

        print("Sound logger ready.")
