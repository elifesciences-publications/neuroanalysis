import sys
from datetime import datetime
from collections import OrderedDict
import numpy as np
import h5py

from .data import Experiment, SyncRecording, PatchClampRecording, Trace
from .test_pulse import PatchClampTestPulse
from . import stimuli


class MiesNwb(Experiment):
    """Class for accessing data from a MIES-generated NWB file.
    """
    def __init__(self, filename):
        Experiment.__init__(self)
        self.filename = filename
        self._hdf = None
        self._sweeps = None
        self._groups = None
        self._notebook = None
        self.open()
        
    @property
    def hdf(self):
        if self._hdf is None:
            self.open()
        return self._hdf

    def notebook(self):
        """Return compiled data from the lab notebook.

        The format is a dict like ``{sweep_number: [ch1, ch2, ...]}`` that contains one key:value
        pair per sweep. Each value is a list containing one metadata dict for each channel in the
        sweep. For example::

            nwb.notebook()[sweep_id][channel_id][metadata_key]
        """
        if self._notebook is None:
            # collect all lab notebook entries
            sweep_entries = OrderedDict()
            tp_entries = []
            device = self.hdf['general/devices'].keys()[0].split('_',1)[-1]
            nb_keys = self.hdf['general']['labnotebook'][device]['numericalKeys'][0]
            nb_fields = OrderedDict([(k, i) for i,k in enumerate(nb_keys)])

            # convert notebook to array here, otherwise we incur the decompression cost for the entire
            # dataset every time we try to access part of it. 
            nb = np.array(self.hdf['general']['labnotebook'][device]['numericalValues'])

            # EntrySourceType field is needed to distinguish between records created by TP vs sweep
            entry_source_type_index = nb_fields.get('EntrySourceType', None)
            
            nb_iter = iter(range(nb.shape[0]))  # so we can skip multiple rows from within the loop
            for i in nb_iter:
                rec = nb[i]
                sweep_num = rec[0,0]

                is_tp_record = False
                is_sweep_record = False

                # ignore records that were generated by test pulse
                # (note: entrySourceType is nan if an older pxp is re-exported to nwb using newer MIES)
                if entry_source_type_index is not None and not np.isnan(rec[entry_source_type_index][0]):
                    if rec[entry_source_type_index][0] == 0:
                        is_sweep_record = True
                    else:
                        is_tp_record = True
                elif i < nb.shape[0] - 1:
                    # Older files may be missing EntrySourceType. In this case, we can identify TP blocks
                    # as two records containing a "TP Peak Resistance" value in the first record followed
                    # by a "TP Pulse Duration" value in the second record.
                    tp_peak = rec[nb_fields['TP Peak Resistance']]
                    if any(np.isfinite(tp_peak)):
                        tp_dur = nb[i+1][nb_fields['TP Pulse Duration']]
                        if any(np.isfinite(tp_dur)):
                            nb_iter.next()
                            is_tp_record = True
                    if not is_tp_record:
                        is_sweep_record = np.isfinite(sweep_num)

                if is_tp_record:
                    rec = np.array(rec)
                    nb_iter.next()
                    rec2 = np.array(nb[i+1])
                    mask = ~np.isnan(rec2)
                    rec[mask] = rec2[mask]
                    tp_entries.append(rec)

                elif is_sweep_record:
                    sweep_num = int(sweep_num)
                    # each sweep gets multiple nb records; for each field we use the last non-nan value in any record
                    if sweep_num not in sweep_entries:
                        sweep_entries[sweep_num] = np.array(rec)
                    else:
                        mask = ~np.isnan(rec)
                        sweep_entries[sweep_num][mask] = rec[mask]

            for swid, entry in sweep_entries.items():
                # last column is "global"; applies to all channels
                mask = ~np.isnan(entry[:,8])
                entry[mask] = entry[:,8:9][mask]
    
                # first 4 fields of first column apply to all channels
                entry[:4] = entry[:4, 0:1]

                # async AD fields (notably used to record temperature) appear
                # only in column 0, but might move to column 8 later? Since these
                # are not channel-specific, we'll copy them to all channels
                for i,k in enumerate(nb_keys):
                    if not k.startswith('Async AD '):
                        continue
                    entry[i] = entry[i, 0]

                # convert to list-o-dicts
                meta = []
                for i in range(entry.shape[1]):
                    tm = entry[:, i]
                    meta.append(OrderedDict([(nb_keys[j], (None if np.isnan(tm[j]) else tm[j])) for j in range(len(nb_keys))]))
                sweep_entries[swid] = meta

            self._notebook = sweep_entries
            self._tp_notebook = tp_entries
            self._notebook_keys = nb_fields
            self._tp_entries = None
        return self._notebook

    @property
    def contents(self):
        """A list of all sweeps in this file.
        """
        if self._sweeps is None:
            sweeps = set()
            for k in self.hdf['acquisition/timeseries'].keys():
                a, b, c = k.split('_')[:3] #discard anything past AD# channel
                sweeps.add(b)
            self._sweeps = [self.create_sync_recording(int(sweep_id)) for sweep_id in sorted(list(sweeps))]
        return self._sweeps
    
    def create_sync_recording(self, sweep_id):
        return MiesSyncRecording(self, sweep_id)

    def close(self):
        self.hdf.close()
        self._hdf = None

    def open(self):
        if self._hdf is not None:
            return
        try:
            self._hdf = h5py.File(self.filename, 'r')
        except Exception:
            print("Error opening: %s" % self.filename)
            raise

    def __enter__(self):
        self.open()
        return self
    
    def __exit__(self, *args):
        self.close()

    @staticmethod
    def pack_sweep_data(sweeps):
        """Return a single array containing all data from a list of sweeps.
        
        The array shape is (sweeps, channels, samples, 2), where the final axis
        contains recorded data at index 0 and the stimulus at index 1.

        All sweeps must have the same length and number of channels.
        """
        sweeps = [s.data() for s in sweeps]
        data = np.empty((len(sweeps),) + sweeps[0].shape, dtype=sweeps[0].dtype)
        for i in range(len(sweeps)):
            data[i] = sweeps[i]
        return data

    @staticmethod
    def igorpro_date(timestamp):
        """Convert an IgorPro timestamp (seconds since 1904-01-01) to a datetime
        object.
        """
        dt = datetime(1970,1,1) - datetime(1904,1,1)
        return datetime.utcfromtimestamp(timestamp) - dt

    @property
    def children(self):
        return self.contents
    
    def __repr__(self):
        return "<%s %s>" % (self.__class__.__name__, self.filename)

    def __getstate__(self):
        state = self.__dict__.copy()
        # don't try to pickle hdf objects
        state['_hdf'] = None
        return state

    def test_pulse_entries(self):
        if self._tp_entries is None:
            self._tp_entries = []
            fields = ['TP Baseline Vm', 'TP Baseline pA', 'TP Peak Resistance', 'TP Steady State Resistance']
            stim_fields = ['TP Baseline Fraction', 'TP Amplitude VC', 'TP Amplitude IC', 'TP Pulse Duration']
            for rec in self._tp_notebook:
                entry = {'timestamp': MiesNwb.igorpro_date(rec[1, 0])}
                for f in fields:
                    i = self._notebook_keys[f]
                    entry[f] = rec[i, :8]
                entry['stim'] = {f:rec[self._notebook_keys[f], 8] for f in stim_fields}
                self._tp_entries.append(entry)
        return self._tp_entries


class MiesTrace(Trace):
    def __init__(self, recording, chan):
        start = recording._meta['start_time']
        
        # Note: this is also available in meta()['Minimum Sampling interval'],
        # but that key is missing in some older NWB files.
        dt = recording.primary_hdf.attrs['IGORWaveScaling'][1,0] / 1000.
        Trace.__init__(self, recording=recording, channel_id=chan, dt=dt, start_time=start)
    
    @property
    def data(self):
        if self._data is None:
            rec = self.recording
            chan = self.channel_id
            if chan == 'primary':
                scale = 1e-12 if rec.clamp_mode == 'vc' else 1e-3
                self._data = np.array(rec.primary_hdf) * scale
            elif chan == 'command':
                scale = 1e-3 if rec.clamp_mode == 'vc' else 1e-12
                # command values are stored _without_ holding, so we add
                # that back in here.
                offset = rec.holding_potential if rec.clamp_mode == 'vc' else rec.holding_current
                self._data = (np.array(rec.command_hdf) * scale) + offset
        return self._data
    
    
class MiesRecording(PatchClampRecording):
    """A single stimulus / recording made on a single channel.
    """
    def __init__(self, sweep, sweep_id, ad_chan):
        self._sweep = sweep
        self._nwb = sweep._nwb
        self._trace_id = (sweep_id, ad_chan)
        self._inserted_test_pulse = None
        self._nearest_test_pulse = None
        self._hdf_group = None
        self._da_chan = None
        headstage_id = int(self.hdf_group['electrode_name'].value[0].split('_')[1])
        
        PatchClampRecording.__init__(self, device_type='MultiClamp 700', device_id=headstage_id,
                                     sync_recording=sweep)

        # update metadata
        nb = self._nwb.notebook()[int(self._trace_id[0])][headstage_id]
        self.meta['holding_potential'] = (
            None if nb['V-Clamp Holding Level'] is None
            else nb['V-Clamp Holding Level'] * 1e-3
        )
        self.meta['holding_current'] = (
            None if nb['I-Clamp Holding Level'] is None
            else nb['I-Clamp Holding Level'] * 1e-12
        )
        self._meta['notebook'] = nb
        if nb['Clamp Mode'] == 0:
            self._meta['clamp_mode'] = 'vc'
        else:
            self._meta['clamp_mode'] = 'ic'
            self._meta['bridge_balance'] = (
                0.0 if nb['Bridge Bal Enable'] == 0.0 or nb['Bridge Bal Value'] is None
                else nb['Bridge Bal Value'] * 1e6
            )
        self._meta['lpf_cutoff'] = nb['LPF Cutoff']
        offset = nb['Pipette Offset']  # sometimes the pipette offset recording can fail??
        self._meta['pipette_offset'] = None if offset is None else offset * 1e-3
        datetime = MiesNwb.igorpro_date(nb['TimeStamp'])
        self.meta['start_time'] = datetime

        self._channels['primary'] = MiesTrace(self, 'primary')
        self._channels['command'] = MiesTrace(self, 'command')

    @property
    def stimulus(self):
        stim = self._meta.get('stimulus', None)
        if stim is None:
            stim_name = self.hdf_group['stimulus_description'].value[0]
            stim = stimuli.Stimulus(description=stim_name)
            if self.has_inserted_test_pulse:
                stim.append_item(self.inserted_test_pulse.stimulus)
            self._meta['stimulus'] = stim
        return stim

    @property
    def hdf_group(self):
        if self._hdf_group is None:
            self._hdf_group = self._nwb.hdf['acquisition/timeseries/data_%05d_AD%d' % self._trace_id]
        return self._hdf_group

    @property
    def clamp_mode(self):
        return 'vc' if self.meta['notebook']['Clamp Mode'] == 0 else 'ic'

    @property
    def primary_hdf(self):
        """The raw HDF5 data containing the primary channel recording
        """
        return self.hdf_group['data']        

    @property
    def command_hdf(self):
        """The raw HDF5 data containing the stimulus command 
        """
        return self._nwb.hdf['stimulus/presentation/data_%05d_DA%d/data' % (self._trace_id[0], self.da_chan())]

    @property
    def nearest_test_pulse(self):
        """The test pulse that was acquired nearest to this recording.
        """
        if self.has_inserted_test_pulse:
            return self.inserted_test_pulse
        else:
            if self._nearest_test_pulse is None:
                self._find_nearest_test_pulse()
            return self._nearest_test_pulse

    def _find_nearest_test_pulse(self):
        start = self.start_time
        min_dt = None
        nearest = None
        for entry in self._nwb.test_pulse_entries():
            dt = abs((entry['timestamp'] - start).total_seconds())
            if min_dt is None or dt < min_dt:
                min_dt = dt
                nearest = entry
        if nearest is None:
            return None

        self._nearest_test_pulse = MiesTestPulse(nearest, self)

    @property
    def has_inserted_test_pulse(self):
        return self.meta['notebook']['TP Insert Checkbox'] == 1.0
    
    @property
    def inserted_test_pulse(self):
        """Return the test pulse inserted at the beginning of the recording,
        or None if no pulse was inserted.
        """
        if self._inserted_test_pulse is None:
            if not self.has_inserted_test_pulse:
                return None
            
            # get start/stop indices of the test pulse region
            pdur = self.meta['notebook']['TP Pulse Duration'] / 1000.
            bdur = pdur / (1.0 - 2. * self.meta['notebook']['TP Baseline Fraction'])
            tdur = pdur + 2 * bdur
            start = 0
            stop = start + int(tdur / self['primary'].dt)
            
            tp = PatchClampTestPulse(self, indices=(start, stop))
            
            # Record amplitude as specified by MIES
            if self.clamp_mode == 'vc':
                amp = self.meta['notebook']['TP Amplitude VC'] * 1e-3
            else:
                amp = self.meta['notebook']['TP Amplitude IC'] * 1e-12

            self._inserted_test_pulse = tp
        return self._inserted_test_pulse

    @property
    def baseline_regions(self):
        """A list of (start, stop) index pairs that cover regions of the recording
        the cell is expected to be in a steady state.
        """
        pri = self['primary']
        dt = pri.dt
        regions = []
        start = self.meta['notebook']['Delay onset auto'] / 1000.  # duration of test pulse
        dur = self.meta['notebook']['Delay onset user'] / 1000.  # duration of baseline
        if dur > 0:
            regions.append((int(start/dt), int(start+dur/dt)))
           
        dur = self.meta['notebook']['Delay termination'] / 1000.
        if dur > 0:
            regions.append((-int(dur/dt), None))
            
        return regions

    def da_chan(self):
        """Return the DA channel ID for this recording.
        """
        if self._da_chan is None:
            hdf = self._nwb.hdf['stimulus/presentation']
            stims = [k for k in hdf.keys() if k.startswith('data_%05d_'%self._trace_id[0])]
            for s in stims:
                elec = hdf[s]['electrode_name'].value[0]
                if elec == 'electrode_%d' % self.device_id:
                    self._da_chan = int(s.split('_')[-1][2:])
            if self._da_chan is None:
                raise Exception("Cannot find DA channel for headstage %d" % self.device_id)
        return self._da_chan

    def _descr(self):
        stim = self.stimulus
        stim_name = '' if stim is None else stim.description
        return "%s %s.%s stim=%s" % (PatchClampRecording._descr(self), self._trace_id[0], self.device_id, stim_name)

    def __getstate__(self):
        state = self.__dict__.copy()
        state['_hdf_group'] = None
        return state


class MiesTestPulse(PatchClampTestPulse):
    def __init__(self, entry, rec):
        chan = rec.device_id
        self._nb_entry = {}
        for k,v in entry.items():
            if isinstance(v, np.ndarray):
                self._nb_entry[k] = v[chan]
            else:
                self._nb_entry[k] = v

        clamp_mode = 'vc' if np.isnan(self._nb_entry['TP Baseline Vm']) else 'ic'
        
        PatchClampRecording.__init__(self,
            device_type=rec.device_type, 
            device_id=rec.device_id,
            start_time=entry['timestamp'],
            channels={},
            clamp_mode=clamp_mode
        )

    @property
    def indices(self):
        return None
        
    @property
    def access_resistance(self):
        """The access resistance measured from this test pulse.
        
        Includes the bridge balance resistance if the recording was made in
        current clamp mode.
        """
        if self.clamp_mode == 'vc':
            return self._nb_entry['TP Peak Resistance'] * 1e6
        else:
            return None
        
    @property
    def input_resistance(self):
        """The input resistance measured from this test pulse.
        """
        return self._nb_entry['TP Steady State Resistance'] * 1e6
    
    @property
    def capacitance(self):
        """The capacitance of the cell measured from this test pulse.
        """
        return None

    @property
    def time_constant(self):
        """The membrane time constant measured from this test pulse.
        """
        return None

    @property
    def baseline_potential(self):
        """The potential of the cell membrane measured (or clamped) before
        the onset of the test pulse.
        """
        if self.clamp_mode == 'ic':
            return self._nb_entry['TP Baseline Vm'] * 1e-3
        else:
            return None  # how do we get the holding potential??
 
    @property
    def baseline_current(self):
        """The pipette current measured (or clamped) before the onset of the
        test pulse.
        """
        if self.clamp_mode == 'vc':
            return self._nb_entry['TP Baseline pA'] * 1e-12
        else:
            return None  # how do we get the holding current??
    

class MiesSyncRecording(SyncRecording):
    """Represents one recorded sweep with multiple channels.
    """
    def __init__(self, nwb, sweep_id):
        sweep_id = int(sweep_id)
        self._nwb = nwb
        self._sweep_id = sweep_id
        self._chan_meta = None
        self._traces = None
        self._notebook_entry = None

        # get list of all A/D channels in this sweep
        chans = []
        for k in self._nwb.hdf['acquisition/timeseries'].keys():
            if not k.startswith('data_%05d_' % sweep_id):
                continue
            chans.append(int(k.split('_')[2][2:]))
        self._ad_channels = sorted(chans)
        
        devs = OrderedDict()

        for ch in self._ad_channels:
            rec = self.create_recording(sweep_id, ch)
            devs[rec.device_id] = rec
        SyncRecording.__init__(self, devs, parent=nwb)
        self._meta['sweep_id'] = sweep_id

    def create_recording(self, sweep_id, ch):
        return MiesRecording(self, sweep_id, ch)
    
    @property
    def key(self):
        return self._sweep_id

    def __repr__(self):
        return "<%s sweep=%d>" % (self.__class__.__name__, self._sweep_id)

    @property
    def parent(self):
        return self._nwb
