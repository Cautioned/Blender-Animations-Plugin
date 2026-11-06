"""Reproduce the native EEVEE distance basis coefficients (numpy required)."""
import numpy as np
cutoffs=np.array([.03,.08,.16,.3,.45,.6,.73,.82,.9,1.])
distance=np.linspace(.015,1,2000)
target=np.clip((1-distance)/.25,0,1)**2
basis=np.maximum(1-(distance[:,None]/cutoffs)**4,0)**2/distance[:,None]**2
coefficients=np.linalg.lstsq(basis,target,rcond=1e-8)[0]
print('Normalized irradiance RMSE:',float(np.mean((basis@coefficients-target)**2)**.5))
for cutoff,power in zip(cutoffs,coefficients):print(f'({cutoff:.8g}, {power:.17g}),')
