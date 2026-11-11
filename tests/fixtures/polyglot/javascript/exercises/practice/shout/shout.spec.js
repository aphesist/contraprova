import { shout } from './shout';

describe('shout', () => {
  test('a word', () => { expect(shout('hi')).toEqual('HI!'); });
  xtest('two words', () => { expect(shout('hi there')).toEqual('HI THERE!'); });
  xtest('empty', () => { expect(shout('')).toEqual('!'); });
  xtest('already upper', () => { expect(shout('OK')).toEqual('OK!'); });
  xtest('digits', () => { expect(shout('a1')).toEqual('A1!'); });
  xtest('punctuation', () => { expect(shout('a,b')).toEqual('A,B!'); });
});
