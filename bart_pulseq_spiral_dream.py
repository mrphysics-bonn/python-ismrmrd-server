
import ismrmrd
import os
import itertools
import logging
import numpy as np
import base64
import ctypes

from bart import bart
from cfft import cfftn, cifftn
from pulseq_prot import insert_hdr, insert_acq, get_ismrmrd_arrays
from reco_helper import calculate_prewhitening, apply_prewhitening, calc_rotmat, pcs_to_gcs, remove_os
from reco_helper import fov_shift_spiral_reapply #, fov_shift_spiral, fov_shift 
from reco_helper import filt_ksp
from DreamMap import calc_fa, DREAM_filter_fid

from skimage import filters
from scipy.ndimage import morphology as morph

""" Spiral subscript to reconstruct dream B1 map
"""

# Folder for sharing data/debugging
shareFolder = "/tmp/share"
debugFolder = os.path.join(shareFolder, "debug")
dependencyFolder = os.path.join(shareFolder, "dependency")

########################
# Main Function
########################

def process_spiral_dream(connection, config, metadata, prot_file):
    
    logging.debug("Spiral DREAM reconstruction")
    
    slc_sel = None

    # Insert protocol header
    insert_hdr(prot_file, metadata)
    
    logging.info("Config: \n%s", config)

    # Check for GPU availability
    if os.environ.get('NVIDIA_VISIBLE_DEVICES') == 'all':
        gpu = True
    else:
        gpu = False

    # Metadata should be MRD formatted header, but may be a string
    # if it failed conversion earlier

    try:
        # logging.info("Metadata: \n%s", metadata.toxml('utf-8'))
        # logging.info("Metadata: \n%s", metadata.serialize())

        logging.info("Incoming dataset contains %d encodings", len(metadata.encoding))
        logging.info("Trajectory type '%s', matrix size (%s x %s x %s), field of view (%s x %s x %s)mm^3", 
            metadata.encoding[0].trajectory.value, 
            metadata.encoding[0].encodedSpace.matrixSize.x, 
            metadata.encoding[0].encodedSpace.matrixSize.y, 
            metadata.encoding[0].encodedSpace.matrixSize.z, 
            metadata.encoding[0].encodedSpace.fieldOfView_mm.x, 
            metadata.encoding[0].encodedSpace.fieldOfView_mm.y, 
            metadata.encoding[0].encodedSpace.fieldOfView_mm.z)

    except:
        logging.info("Improperly formatted metadata: \n%s", metadata)
    
    # # Initialize lists for datasets
    n_slc = metadata.encoding[0].encodingLimits.slice.maximum + 1
    n_contr = metadata.encoding[0].encodingLimits.contrast.maximum + 1

    acqGroup = [[[] for _ in range(n_slc)] for _ in range(n_contr)]
    noiseGroup = []
    waveformGroup = []

    acsGroup = [[] for _ in range(n_slc)]
    sensmaps = [None] * n_slc
    old_grid = []
    dmtx = None
    refscans = [None] * n_slc

    # read protocol arrays
    prot_arrays = get_ismrmrd_arrays(prot_file)

    # for B1 Dream map
    process_raw.imagesets = [None] * n_contr
    process_raw.rawdata = [None] * n_contr
    
    # different contrasts need different scaling
    process_raw.imascale = [None] * n_contr
    
    # parameters for reapplying FOV shift
    nsegments = metadata.encoding[0].encodingLimits.segment.maximum + 1
    matr_sz = np.array([metadata.encoding[0].encodedSpace.matrixSize.x, metadata.encoding[0].encodedSpace.matrixSize.y])
    res = np.array([metadata.encoding[0].encodedSpace.fieldOfView_mm.x / matr_sz[0], metadata.encoding[0].encodedSpace.fieldOfView_mm.y / matr_sz[1], 1])

    base_trj = None
    try:
        for acq_ctr, item in enumerate(connection):

            # ----------------------------------------------------------
            # Raw k-space data messages
            # ----------------------------------------------------------
            if isinstance(item, ismrmrd.Acquisition):

                # insert acquisition protocol
                # base_trj is used to correct FOV shift (see below)
                base_traj = insert_acq(prot_file, item, acq_ctr, metadata)
                if base_traj is not None:
                    base_trj = base_traj

                # run noise decorrelation
                if item.is_flag_set(ismrmrd.ACQ_IS_NOISE_MEASUREMENT):
                    noiseGroup.append(item)
                    continue
                elif len(noiseGroup) > 0 and dmtx is None:
                    noise_data = []
                    for acq in noiseGroup:
                        noise_data.append(acq.data)
                    noise_data = np.concatenate(noise_data, axis=1)
                    # calculate pre-whitening matrix
                    dmtx = calculate_prewhitening(noise_data)
                    del(noise_data)
                    noiseGroup.clear()
                    
                # skip slices in single slice reconstruction
                if slc_sel is not None and item.idx.slice != slc_sel:
                    continue
                
                # Accumulate all imaging readouts in a group
                if item.is_flag_set(ismrmrd.ACQ_IS_PHASECORR_DATA):
                    continue
                elif item.is_flag_set(ismrmrd.ACQ_IS_DUMMYSCAN_DATA): # skope sync scans
                    continue
                elif item.is_flag_set(ismrmrd.ACQ_IS_PARALLEL_CALIBRATION):
                    acsGroup[item.idx.slice].append(item)
                    if item.is_flag_set(ismrmrd.ACQ_LAST_IN_SLICE):
                        sensmaps[item.idx.slice] = process_acs(acsGroup[item.idx.slice], metadata, dmtx, gpu)
                        old_grid.append(item.idx.slice)
                        acsGroup[item.idx.slice].clear()
                    continue
                
                # Copy sensitivity maps if less slices were acquired
                n_sensmaps = len([x for x in sensmaps if x is not None])
                if (n_sensmaps != len(sensmaps) and n_sensmaps != 0):                    
                    if (n_slc % 2 == 0):
                        for i in range(0,n_slc-1,2):
                            sensmaps[i] = sensmaps[i+1].copy()
                    else:
                        for i in range(1,n_slc,2):
                            sensmaps[i] = sensmaps[i-1].copy()
                    
                if item.idx.segment == 0:
                    acqGroup[item.idx.contrast][item.idx.slice].append(item)

                    # for reapplying FOV shift (see below)
                    pred_trj = item.traj[:]
                    rotmat = calc_rotmat(item)
                    shift = pcs_to_gcs(np.asarray(item.position), rotmat) / res
                else:
                    # append data to first segment of ADC group
                    idx_lower = item.idx.segment * item.number_of_samples
                    idx_upper = (item.idx.segment+1) * item.number_of_samples
                    acqGroup[item.idx.contrast][item.idx.slice][-1].data[:,idx_lower:idx_upper] = item.data[:]
                if item.idx.segment == nsegments - 1:
                    # Reapply FOV Shift with predicted trajectory
                    sig = acqGroup[item.idx.contrast][item.idx.slice][-1].data[:]
                    acqGroup[item.idx.contrast][item.idx.slice][-1].data[:] = fov_shift_spiral_reapply(sig, pred_trj, base_trj, shift, matr_sz)

                # When this criteria is met, run process_raw() on the accumulated
                # data, which returns images that are sent back to the client.
                if item.is_flag_set(ismrmrd.ACQ_LAST_IN_SLICE) or item.is_flag_set(ismrmrd.ACQ_LAST_IN_REPETITION):
                    logging.info("Processing a group of k-space data")
                    images = process_raw(acqGroup[item.idx.contrast][item.idx.slice], metadata, dmtx, sensmaps[item.idx.slice], gpu, prot_arrays)
                    logging.debug("Sending images to client:\n%s", images)
                    connection.send_image(images)
                    acqGroup[item.idx.contrast][item.idx.slice].clear() # free memory

            # ----------------------------------------------------------
            # Image data messages
            # ----------------------------------------------------------
            elif isinstance(item, ismrmrd.Image):
                # just pass along
                connection.send_image(item)
                continue

            # ----------------------------------------------------------
            # Waveform data messages
            # ----------------------------------------------------------
            elif isinstance(item, ismrmrd.Waveform):
                waveformGroup.append(item)

            elif item is None:
                break

            else:
                logging.error("Unsupported data type %s", type(item).__name__)

        # Extract raw ECG waveform data. Basic sorting to make sure that data 
        # is time-ordered, but no additional checking for missing data.
        # ecgData has shape (5 x timepoints)
        if len(waveformGroup) > 0:
            waveformGroup.sort(key = lambda item: item.time_stamp)
            ecgData = [item.data for item in waveformGroup if item.waveform_id == 0]
            ecgData = np.concatenate(ecgData,1)

        # Process any remaining groups of raw or image data.  This can 
        # happen if the trigger condition for these groups are not met.
        # This is also a fallback for handling image data, as the last
        # image in a series is typically not separately flagged.
        if item is not None:
            if len(acqGroup[item.idx.contrast][item.idx.slice]) > 0:
                logging.info("Processing a group of k-space data (untriggered)")
                if sensmaps[item.idx.slice] is None:
                    # run parallel imaging calibration
                    sensmaps[item.idx.slice] = process_acs(acsGroup[item.idx.slice], metadata, dmtx) 
                image = process_raw(acqGroup[item.idx.contrast][item.idx.slice], metadata, dmtx, sensmaps[item.idx.slice])
                logging.debug("Sending image to client:\n%s", image)
                connection.send_image(image)
                acqGroup = []

    finally:
        connection.send_close()

# %%
#########################
# Process Data
#########################

def process_raw(group, metadata, dmtx=None, sensmaps=None, gpu=False, prot_arrays=None):

    nx = metadata.encoding[0].encodedSpace.matrixSize.x
    ny = metadata.encoding[0].encodedSpace.matrixSize.y
    nz = metadata.encoding[0].encodedSpace.matrixSize.z
    
    rNx = metadata.encoding[0].reconSpace.matrixSize.x
    rNy = metadata.encoding[0].reconSpace.matrixSize.y
    rNz = metadata.encoding[0].reconSpace.matrixSize.z

    data, trj = sort_spiral_data(group, metadata, dmtx)
    
    if gpu:
        nufft_config = 'nufft -g -i -l 0.005 -t -d %d:%d:%d'%(nx, nx, nz)
        ecalib_config = 'ecalib -m 1 -I -r 20 -k 6'
        pics_config = 'pics -g -S -e -l1 -r 0.001 -i 50 -t'
    else:
        nufft_config = 'nufft -i -m 20 -l 0.005 -c -t -d %d:%d:%d'%(nx, nx, nz)
        ecalib_config = 'ecalib -m 1 -I -r 20 -k 6'
        pics_config = 'pics -S -e -l1 -r 0.001 -i 50 -t'

    # B1 Map calculation (Dream approach)
    # dream array : [ste_contr,flip_angle_ste,TR,flip_angle,prepscans,t1]
    dream = prot_arrays['dream']
    n_contr = metadata.encoding[0].encodingLimits.contrast.maximum + 1
    process_raw.rawdata[group[0].idx.contrast] = data.copy() # complex rawdata of the two contrasts
    
    if (group[0].idx.contrast == int(n_contr-1-dream[0])) and (dream.size > 2): # if not Ste and global filter is activated -> no reconstruction of unfiltered fid
        logging.info("fid filter activated")
    else:
        if sensmaps is None:                
            # bart nufft
            data = bart(1, nufft_config, trj, data)
            # Sum of squares coil combination
            data = np.sqrt(np.sum(np.abs(data)**2, axis=-1))
        else:
            data = bart(1, pics_config , trj, data, sensmaps)
            data = np.abs(data)
            # make sure that data is at least 3d:
        while np.ndim(data) < 3:
            data = data[..., np.newaxis]
        if nz > rNz:
            # remove oversampling in slice direction
            data = data[:,:,(nz - rNz)//2:-(nz - rNz)//2]
    
        logging.debug("Image data is size %s" % (data.shape,))
    
    process_raw.imagesets[group[0].idx.contrast] = data.copy()
    full_set_check = all(elem is not None for elem in process_raw.imagesets)
    if full_set_check:
        logging.info("B1 map calculation using Dream")
        ste = np.asarray(process_raw.imagesets[int(dream[0])])
        
        if dream.size > 2 : # without filter: dream = ([ste_contr,flip_angle_ste])
            logging.info("Global filter approach")
            # Blurring compensation parameters
            alpha = dream[1]     # preparation FA
            tr = dream[2]        # [s]
            beta = dream[3]      # readout FA
            dummies = dream[4]   # number of dummy scans before readout echo train starts
            # T1 estimate:
            t1 = dream[5]        # [s] - approximately Gufi Phantom at 7T
            # TI estimate (the time after DREAM preparation after which each k-space line is acquired):
            ti = 0
            ste_data = np.asarray(process_raw.rawdata[int(dream[0])])
            fid_data = np.asarray(process_raw.rawdata[int(n_contr-1-dream[0])])
            mean_alpha = calc_fa(ste_data.mean(), fid_data.mean())

            mean_beta = mean_alpha / alpha * beta
            for i,acq in enumerate(group):
                ti = tr * (dummies + i) # [s]
                # Global filter:
                filt = DREAM_filter_fid(mean_alpha, mean_beta, tr, t1, ti)
                # apply filter:
                fid_data[:,:,i,:] *= filt
            # reco of fid:
            if sensmaps is None:
                fid = bart(1, nufft_config, trj, fid_data)
                fid = np.sqrt(np.sum(np.abs(fid)**2, axis=-1))
            else:
                fid = bart(1, pics_config , trj, fid_data, sensmaps)
                fid = np.abs(fid)
            while np.ndim(fid) < 3:
                    fid = fid[..., np.newaxis]         
            if nz > rNz:
                fid = fid[:,:,(nz - rNz)//2:-(nz - rNz)//2]
            # fa map:
            fa_map = calc_fa(abs(ste), abs(fid))
            data = fid.copy()
        else:
            fid = np.asarray(process_raw.imagesets[int(n_contr-1-dream[0])]) # fid unchanged
       
        # image mask for fa-map (use of filtered or unfiltered fid) - not used atm
        mask = np.zeros(fid.shape)
        for nz in range(fid.shape[-1]):
            # otsu
            val = filters.threshold_otsu(fid[:,:,nz])
            otsu = fid[:,:,nz] > val
            # fill holes
            imfill = morph.binary_fill_holes(otsu) * 1
            # dilation
            mask[:,:,nz] = morph.binary_dilation(imfill)
                
        # fa map:
        fa_map = calc_fa(abs(ste), abs(fid))
        # fa_map *= mask
        current_refvolt = metadata.userParameters.userParameterDouble[5].value
        nom_fa = dream[1]
        logging.info("current_refvolt = %sV and nom_fa = %s°^" % (current_refvolt, nom_fa))
        ref_volt = current_refvolt * (nom_fa/fa_map)
        
        fa_map *= 10 # increase dynamic range
        fa_map = np.around(fa_map)
        fa_map = fa_map.astype(np.int16)
        logging.debug("fa map is size %s" % (fa_map.shape,))
        
        ref_volt *= 10
        ref_volt = np.around(ref_volt)
        ref_volt = ref_volt.astype(np.int16)
        logging.debug("ref_volt map is size %s" % (ref_volt.shape,))
        
        process_raw.imagesets = [None] * n_contr # free list
        process_raw.rawdata   = [None] * n_contr # free list

        # correct orientation at scanner (consistent with ICE)
        fa_map = np.swapaxes(fa_map, 0, 1)
        fa_map = np.flip(fa_map, (0,1,2))
        ref_volt = np.swapaxes(ref_volt, 0, 1)
        ref_volt = np.flip(ref_volt, (0,1,2))
    else:
        fa_map = None
        ref_volt = None
    
    # correct orientation at scanner (consistent with ICE)
    data = np.swapaxes(data, 0, 1)
    data = np.flip(data, (0,1,2))

    # Normalize and convert to int16
    #save one scaling in 'static' variable
    contr = group[0].idx.contrast
    if process_raw.imascale[contr] is None:
        process_raw.imascale[contr] = 0.8 / data.max()
    data *= 32767 * process_raw.imascale[contr]
    data = np.around(data)
    data = data.astype(np.int16)

    # Set ISMRMRD Meta Attributes
    meta = ismrmrd.Meta({'DataRole':               'Image',
                         'ImageProcessingHistory': ['FIRE', 'PYTHON'],
                         'WindowCenter':           '16384',
                         'WindowWidth':            '32768',
                         'Keep_image_geometry':    '1'})
    xml = meta.serialize()

    meta2 = ismrmrd.Meta({'DataRole':               'Image',
                         'ImageProcessingHistory': ['FIRE', 'PYTHON'],
                         'WindowCenter':           '512',
                         'WindowWidth':            '1024',
                         'Keep_image_geometry':    '1'})
    xml2 = meta2.serialize()

    meta3 = ismrmrd.Meta({'DataRole':               'Image',
                         'ImageProcessingHistory': ['FIRE', 'PYTHON'],
                         'WindowCenter':           '4096',
                         'WindowWidth':            '8192',
                         'Keep_image_geometry':    '1'})
    xml3 = meta3.serialize()
    
    images = []
    n_par = data.shape[-1]
    n_slc = metadata.encoding[0].encodingLimits.slice.maximum + 1
    n_contr = metadata.encoding[0].encodingLimits.contrast.maximum + 1
    
    # Format as ISMRMRD image data
    if n_par > 1:
        for par in range(n_par):
            image = ismrmrd.Image.from_array(data[...,par], acquisition=group[0])
            image.image_index = 1 + group[0].idx.contrast * n_par + par
            image.image_series_index = 1
            image.slice = 0
            image.attribute_string = xml
            image.field_of_view = (ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.x), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.y), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.z))
            images.append(image)
        
        if fa_map is not None:
            for par in range(n_par):
                image = ismrmrd.Image.from_array(fa_map[...,par], acquisition=group[0])
                image.image_index = 1 + par
                image.image_series_index = 2
                image.slice = 0
                image.attribute_string = xml2
                image.field_of_view = (ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.x), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.y), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.z))
                images.append(image)
        
        if ref_volt is not None:
            for par in range(n_par):
                image = ismrmrd.Image.from_array(ref_volt[...,par], acquisition=group[0])
                image.image_index = 1 + par
                image.image_series_index = 3
                image.slice = 0
                image.attribute_string = xml3
                image.field_of_view = (ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.x), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.y), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.z))
                images.append(image)
        
    else:
        image = ismrmrd.Image.from_array(data[...,0], acquisition=group[0])
        image.image_index = 1 + group[0].idx.contrast * n_slc + group[0].idx.slice # contains image index (slices/partitions)
        image.image_series_index = 1 + group[0].idx.repetition # contains image series index, e.g. different contrasts
        image.slice = 0
        image.attribute_string = xml
        image.field_of_view = (ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.x), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.y), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.z))
        images.append(image)
        
        if fa_map is not None:
            image = ismrmrd.Image.from_array(fa_map[...,0], acquisition=group[0])
            image.image_index = 1 + group[0].idx.contrast * n_slc + group[0].idx.slice
            image.image_series_index = 2
            image.slice = 0
            image.attribute_string = xml2
            image.field_of_view = (ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.x), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.y), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.z))
            images.append(image)
        
        if ref_volt is not None:
            image = ismrmrd.Image.from_array(ref_volt[...,0], acquisition=group[0])
            image.image_index = 1 + group[0].idx.contrast * n_slc + group[0].idx.slice
            image.image_series_index = 3
            image.slice = 0
            image.attribute_string = xml3
            image.field_of_view = (ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.x), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.y), 
                                ctypes.c_float(metadata.encoding[0].reconSpace.fieldOfView_mm.z))
            images.append(image)

    return images

def process_acs(group, metadata, dmtx=None, gpu=False):
    if len(group)>0:
        data = sort_into_kspace(group, metadata, dmtx, zf_around_center=True)
        data = remove_os(data)

        #--- FOV shift is done in the Pulseq sequence by tuning the ADC frequency   ---#
        #--- However leave this code to fall back to reco shifts, if problems occur ---#
        #--- and for reconstruction of old data                                     ---#
        # rotmat = calc_rotmat(group[0])
        # if not rotmat.any(): rotmat = -1*np.eye(3) # compatibility if refscan rotmat is not in protocol, this is the standard Pulseq rotation matrix
        # res = metadata.encoding[0].encodedSpace.fieldOfView_mm.x / metadata.encoding[0].encodedSpace.matrixSize.x
        # shift = pcs_to_gcs(np.asarray(group[0].position), rotmat) / res
        # data = fov_shift(data, shift)

        if gpu:
            sensmaps = bart(1, 'ecalib -g -m 1 -k 6 -I', data)  # ESPIRiT calibration
        else:
            sensmaps = bart(1, 'ecalib -m 1 -k 6 -I', data)  # ESPIRiT calibration
        np.save(debugFolder + "/" + "acs.npy", data)
        np.save(debugFolder + "/" + "sensmaps.npy", sensmaps)
        return sensmaps
    else:
        return None

# %%
#########################
# Sort Data
#########################

def sort_spiral_data(group, metadata, dmtx=None):
    
    nx = metadata.encoding[0].encodedSpace.matrixSize.x
    nz = metadata.encoding[0].encodedSpace.matrixSize.z
    res = metadata.encoding[0].reconSpace.fieldOfView_mm.x / metadata.encoding[0].encodedSpace.matrixSize.x
    rot_mat = calc_rotmat(group[0])

    sig = list()
    trj = list()
    enc = list()   
    for acq in group:

        enc1 = acq.idx.kspace_encode_step_1
        enc2 = acq.idx.kspace_encode_step_2
        kz = enc2 - nz//2
        enc.append([enc1, enc2])
        
        # append data after optional prewhitening
        if dmtx is None:
            sig.append(acq.data)
        else:
            sig.append(apply_prewhitening(acq.data, dmtx))

        # update trajectory
        traj = np.swapaxes(acq.traj[:,:3],0,1) # [samples, dims] to [dims, samples]
        trj.append(traj)
        
        #--- FOV shift is done in the Pulseq sequence by tuning the ADC frequency   ---#
        #--- However leave this code to fall back to reco shifts, if problems occur ---#
        #--- and for reconstruction of old data                                     ---#
        # shift = pcs_to_gcs(np.asarray(acq.position), rot_mat) / res
        # sig[-1] = fov_shift_spiral(sig[-1], traj, shift, nx)
        
    np.save(debugFolder + "/" + "enc.npy", enc)
    
    # convert lists to numpy arrays
    trj = np.asarray(trj) # current size: (nacq, 3, ncol)
    sig = np.asarray(sig) # current size: (nacq, ncha, ncol)
    
    # readout filter to remove Gibbs ringing
    for nacq in range(sig.shape[0]):
        sig[nacq,:,:] = filt_ksp(kspace=sig[nacq,:,:], traj=trj[nacq,:,:], filt_fac=1)
    
    # rearrange trj & sig for bart
    trj = np.transpose(trj, [1, 2, 0]) # [3, ncol, nacq]
    sig = np.transpose(sig, [2, 0, 1])[np.newaxis] # [1, ncol, nacq, ncha]
    logging.debug("Trajectory shape = %s , Signal Shape = %s "%(trj.shape, sig.shape))
    
    np.save(debugFolder + "/" + "trj.npy", trj)

    return sig, trj

def sort_into_kspace(group, metadata, dmtx=None, zf_around_center=False):
    # initialize k-space
    nc = metadata.acquisitionSystemInformation.receiverChannels

    enc1_min, enc1_max = int(999), int(0)
    enc2_min, enc2_max = int(999), int(0)
    for acq in group:
        enc1 = acq.idx.kspace_encode_step_1
        enc2 = acq.idx.kspace_encode_step_2
        if enc1 < enc1_min:
            enc1_min = enc1
        if enc1 > enc1_max:
            enc1_max = enc1
        if enc2 < enc2_min:
            enc2_min = enc2
        if enc2 > enc2_max:
            enc2_max = enc2

    nx = 2 * metadata.encoding[0].encodedSpace.matrixSize.x
    ny = metadata.encoding[0].encodedSpace.matrixSize.x
    nz = metadata.encoding[0].encodedSpace.matrixSize.z

    kspace = np.zeros([ny, nz, nc, nx], dtype=group[0].data.dtype)
    counter = np.zeros([ny, nz], dtype=np.uint16)

    logging.debug("nx/ny/nz: %s/%s/%s; enc1 min/max: %s/%s; enc2 min/max:%s/%s, ncol: %s" % (nx, ny, nz, enc1_min, enc1_max, enc2_min, enc2_max, group[0].data.shape[-1]))
    
    for acq in group:
        enc1 = acq.idx.kspace_encode_step_1
        enc2 = acq.idx.kspace_encode_step_2

        # in case dim sizes smaller than expected, sort data into k-space center (e.g. for reference scans)
        ncol = acq.data.shape[-1]
        cx = nx // 2
        ccol = ncol // 2
        col = slice(cx - ccol, cx + ccol)

        if zf_around_center:
            cy = ny // 2
            cz = nz // 2

            cenc1 = (enc1_max+1) // 2
            cenc2 = (enc2_max+1) // 2

            # sort data into center k-space (assuming a symmetric acquisition)
            enc1 += cy - cenc1
            enc2 += cz - cenc2
        
        if dmtx is None:
            kspace[enc1, enc2, :, col] += acq.data
        else:
            kspace[enc1, enc2, :, col] += apply_prewhitening(acq.data, dmtx)
        counter[enc1, enc2] += 1

    # support averaging (with or without acquisition weighting)
    kspace /= np.maximum(1, counter[:,:,np.newaxis,np.newaxis])

    # rearrange kspace for bart - target size: (nx, ny, nz, nc)
    kspace = np.transpose(kspace, [3, 0, 1, 2])

    return kspace
