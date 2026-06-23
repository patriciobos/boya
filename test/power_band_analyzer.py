# -*- coding: utf-8 -*-
"""
Created on Thu Nov  6 00:35:38 2025

@author: MarianoCinquini
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

test = pd.read_csv("C:/repo/boya/data/20180824_8105_20m_daspre_cap_2.csv")

freq_bands = pd.read_csv("C:/repo/boya/support/third_octave_bands.csv")
freq_bands = freq_bands.iloc[0:-1]

min_BL = np.loadtxt("C:/repo/boya/support/reference_minimum_BL.txt")


psd = (test["rel_power_dB_ch1"].values + min_BL) - 10 * np.log10(
    freq_bands["fh"].values - freq_bands["fl"].values
)

plt.figure()
plt.semilogx(freq_bands["fc"], psd, color="red", marker="o")
plt.grid()
plt.ylim([30, 130])
plt.xlabel("Frequency (Hz)")
plt.ylabel("PSD (dB re. 1uPa^2/Hz)")
